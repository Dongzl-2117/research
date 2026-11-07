"""译制闭环：结构是硬要求，数字事实另做独立核验。

占位、链接、图片、代码、条数不对，只修这一段一次。
解析器只提供线索，解析对不上不等于事实错，解析对上也不单独放行多数字的句子。
独立核验要交回能在原文和译文里逐字找到的片段，以及等价、冲突或不确定。
没有片段的布尔结论不能通过。片段里的钟点、日期、月份、财季、金额若互相矛盾，也不能通过。
没通过只修一次，再用同一种核验复核。没入库的各阶段都留下，不含密钥。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from researchlib.translate.llm import LLMTransportError, output_tokens
from researchlib.translate.numbers import _clocks_match, _extract, _text
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.provenance import leaf_problem, numeric_anchors
from researchlib.translate.segments import Shielded, unshield

logger = logging.getLogger("researchlib.translate")

_REPAIR_NOTE = (
    "这一段的数字证据没有对上。不要自己宣布通过。"
    "只改证据里互相矛盾的单位、数量级、数值或对象；写法不用改。"
    "若只是不确定，把译文原样放回 t。只输出 JSON，t 里只能有这一段。"
)
_UNSHIELD_NOTE = (
    "这一段的图片、代码占位或链接编号丢了。不要自己宣布通过。"
    "⟦n⟧、[[n]]、【n】 和 <a id=\"Ln\"> 按原文各留一次，编号不能改。"
    "只输出 JSON，t 里只能有这一段。"
)
_UNIT_GAP = re.compile(r"(?<=\d)\s+(?=个月|星期|小时|天|周)")
_TRACKED = ("clock", "date", "month", "fiscal", "duration", "ratio")
_COVER = ("clock", "date", "month", "fiscal", "qty", "duration", "ratio")
_VERIFY_SYSTEM = (
    "你核对英文片段和中文译文是否同一批事实。不要翻译，不要改写，不要宣布检查通过。\n"
    "只输出 JSON："
    '{"evidence_id":"原样带回","verdict":"equivalent|conflict|uncertain",'
    '"claims":[{"source":"英文里逐字存在的片段","target":"中文里逐字存在的片段",'
    '"value":"这个数或时间","object_source":"英文片段里的对象","object_target":"中文片段里的对象",'
    '"relation":"比例、起止、时间或空","same":true}]}\n'
    "equivalent 和 conflict 都必须有 claims。source、target 必须能在对应一侧逐字找到。\n"
    "数字写法和中文语序可以不同。单位、数量级、钟点上下午、日期、月份、财季、对象或关系不同就是 conflict。\n"
    "每一笔硬事实都要有一条 claim。看不出就 uncertain。"
)


@dataclass
class Judgment:
    status: str
    reason: str


def _fold(html: str) -> str:
    return _UNIT_GAP.sub("", html or "")


def _keys(html: str, kind: str) -> list[str]:
    return [fact.key for fact in _extract(html) if fact.hard and fact.kind == kind]


def _reason(source: str, target: str) -> str:
    """检查没通过时给原因。每一类硬事实都看，不跳过钟点、日期、月份、财季。"""
    for kind in _TRACKED:
        if _keys(source, kind) != _keys(target, kind):
            return "conflict"
    source_qty, target_qty = _keys(source, "qty"), _keys(target, "qty")
    if source_qty != target_qty:
        if sorted(source_qty) == sorted(target_qty) and source_qty:
            return "binding"
        anchors = numeric_anchors(source)
        if anchors and anchors == numeric_anchors(target) and (source_qty or target_qty):
            return "scale"
        return "conflict"
    return "uncertain"


def judge_numbers(source: str, target: str) -> Judgment:
    """通过只表示全量检查通过。没通过不会因为某一类事实没被收进证据就放过。"""
    source, target = _fold(source), _fold(target)
    problem = leaf_problem(source, target)
    if problem is None:
        return Judgment("accept", "ok")
    if problem != "numbers":
        return Judgment("contradict", "conflict")
    reason = _reason(source, target)
    if reason == "uncertain":
        return Judgment("uncertain", "uncertain")
    return Judgment("contradict", reason)


def semantic_ok(source: str, target: str) -> bool:
    return judge_numbers(source, target).status == "accept"


def numeric_verdict(source: str, target: str) -> str:
    found = judge_numbers(source, target)
    if found.status == "accept":
        return "ok"
    if found.status == "uncertain":
        return "uncertain"
    return found.reason


def fact_brief(source: str, target: str) -> str:
    """给日志和修复请求的证据摘要。只留种类和键。"""

    def brief(html: str) -> str:
        bits = []
        for fact in _extract(_fold(html)):
            mark = "" if fact.hard else "~"
            bits.append(f"{mark}{fact.kind}:{fact.key}")
            if len(bits) >= 12:
                break
        return " ".join(bits)

    found = judge_numbers(source, target)
    return f"{found.status}:{found.reason} {brief(source)} => {brief(target)}"[:240]


def note_reject(
    data_dir: Path,
    *,
    article_id: int,
    slot: str,
    kind: str,
    source: str,
    draft: str,
    stages: list[dict] | None = None,
) -> None:
    """未入库的每一阶段都写下。draft 是最后一稿，不是只留最初那稿。不含密钥。"""
    directory = Path(data_dir) / "translate-rejects"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "article_id": article_id,
        "slot": slot,
        "kind": kind,
        "source": source,
        "draft": draft,
        "stages": stages or [],
        "facts": fact_brief(source, draft),
    }
    with (directory / f"{article_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.warning("译制未入库 article=%s slot=%s kind=%s", article_id, slot, kind)


def _bound_html(item: object, segment_id: str, source_hash: str) -> tuple[str | None, str]:
    """带了段身份就必须回显同一个 id 和 source_hash。缺一不可，字符串不算。"""
    if not isinstance(item, dict) or not isinstance(item.get("html"), str) or not item["html"].strip():
        return None, "response-identity-missing"
    if not isinstance(item.get("id"), str) or not item["id"]:
        return None, "response-identity-missing"
    if item["id"] != segment_id:
        return None, "response-id-order"
    if item.get("source_hash") != source_hash:
        return None, "response-source-hash"
    return item["html"], ""


def repair_slot(
    source: str,
    draft: str,
    data_dir: Path,
    *,
    note: str = _REPAIR_NOTE,
    segment_id: str | None = None,
    source_hash: str | None = None,
) -> tuple[str, str, str | None]:
    """再要一稿。返回 (采用的译文, 身份错误码, 被丢掉的模型稿)。

    采用的译文还要再检查，不是通过证明。temperature 0.3 与生成相同；
    DeepSeek 思考模式的请求不带这个字段。
    """
    user = {
        "segments": [source],
        "repair": note + "\n证据: " + fact_brief(source, draft) + "\n译文:\n" + draft[:4000],
    }
    if segment_id and source_hash:
        user["segment_ids"] = [segment_id]
        user["source_hashes"] = [source_hash]
    import researchlib.translate.service as translate_service

    result = translate_service.chat_json(
        system=SYSTEM_BODY,
        user=user,
        purpose="translate_body",
        subject="batch",
        temperature=0.3,
        max_tokens=min(output_tokens(), max(8000, len(draft) * 2 + 800)),
        data_dir=data_dir,
    )
    raw = result.data.get("t") if isinstance(result.data, dict) else None
    if not isinstance(raw, list) or len(raw) != 1:
        return draft, "response-count", None
    item = raw[0]
    if segment_id and source_hash:
        html, problem = _bound_html(item, segment_id, source_hash)
        if problem:
            seen = item.get("html") if isinstance(item, dict) else item if isinstance(item, str) else None
            return draft, problem, seen if isinstance(seen, str) else None
        return html or draft, "", None
    if isinstance(item, str) and item.strip():
        return item, "", None
    return draft, "response-identity-missing", None


def _code(reason: str) -> str:
    if reason == "scale":
        return "numbers-scale"
    if reason == "binding":
        return "numbers-binding"
    if reason == "uncertain":
        return "numbers-uncertain"
    return "numbers"


def evidence_id(source: str, target: str) -> str:
    raw = (_fold(source) + "\n" + _fold(target)).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _visible(html: str) -> str:
    try:
        return _text(html or "")
    except Exception:
        return html or ""


def _quoted(html: str, quote: str) -> bool:
    if quote in (html or "") or quote in _visible(html):
        return True
    folded = _fold(html)
    return _fold(quote) in folded or _fold(quote) in _visible(folded)


def _hard_keys(html: str, kinds: tuple[str, ...]) -> list[tuple[str, str]]:
    found = []
    for fact in _extract(_fold(html)):
        if fact.hard and fact.kind in kinds:
            found.append((fact.kind, fact.key))
    return found


def _span_conflict(source_quote: str, target_quote: str) -> str | None:
    """片段里两边都解析出的钟点、日期、月份、财季、时段、比例、金额必须相容。一边没解析出来只是写法。"""
    for kind in ("date", "month", "fiscal", "duration", "ratio"):
        left = [key for item, key in _hard_keys(source_quote, (kind,))]
        right = [key for item, key in _hard_keys(target_quote, (kind,))]
        if left and right and sorted(left) != sorted(right):
            return "conflict"
    left_clock = [key for _item, key in _hard_keys(source_quote, ("clock",))]
    right_clock = [key for _item, key in _hard_keys(target_quote, ("clock",))]
    if left_clock and right_clock:
        if not (len(left_clock) == len(right_clock) == 1 and _clocks_match(left_clock[0], right_clock[0])):
            if left_clock != right_clock:
                return "conflict"
    left_qty = [key for _item, key in _hard_keys(source_quote, ("qty",))]
    right_qty = [key for _item, key in _hard_keys(target_quote, ("qty",))]
    if left_qty and right_qty and sorted(left_qty) != sorted(right_qty):
        anchors = numeric_anchors(source_quote)
        if anchors and anchors == numeric_anchors(target_quote):
            return "scale"
        return "conflict"
    kinds = ("qty", "literal", "clock", "date", "fiscal", "duration", "ratio")
    left = [key for _kind, key in _hard_keys(source_quote, kinds)]
    right = [key for _kind, key in _hard_keys(target_quote, kinds)]
    if left and right and not _related(left, right):
        return "conflict"
    return None


def _related(left: list[str], right: list[str]) -> bool:
    if set(left) & set(right):
        return True
    for key in (*left, *right):
        if "/" not in key:
            continue
        parts = set(key.split("/"))
        other = right if key in left else left
        if parts & set(other):
            return True
    return False


def _covers(html: str, quotes: list[str]) -> bool:
    needed: dict[tuple[str, str], int] = {}
    for item in _hard_keys(html, _COVER):
        needed[item] = needed.get(item, 0) + 1
    if not needed:
        return True
    found: dict[tuple[str, str], int] = {}
    for quote in quotes:
        for item in _hard_keys(quote, _COVER):
            found[item] = found.get(item, 0) + 1
    return all(found.get(item, 0) >= count for item, count in needed.items())


def apply_report(source: str, target: str, data: object) -> Judgment:
    """核验结论必须带回 evidence_id 和能对上原文的片段。没有依据就不能通过。"""
    if not isinstance(data, dict) or data.get("evidence_id") != evidence_id(source, target):
        return Judgment("uncertain", "evidence-id")
    verdict = data.get("verdict")
    claims = data.get("claims")
    if verdict not in {"equivalent", "conflict", "uncertain"} or not isinstance(claims, list):
        return Judgment("uncertain", "evidence-missing")
    if verdict == "uncertain":
        return Judgment("uncertain", "uncertain")
    if not claims:
        return Judgment("uncertain", "evidence-missing")
    source_quotes: list[str] = []
    target_quotes: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            return Judgment("uncertain", "evidence-missing")
        source_quote, target_quote = claim.get("source"), claim.get("target")
        if not isinstance(source_quote, str) or not isinstance(target_quote, str):
            return Judgment("uncertain", "evidence-missing")
        if not source_quote.strip() or not target_quote.strip():
            return Judgment("uncertain", "evidence-missing")
        if not _quoted(source, source_quote) or not _quoted(target, target_quote):
            return Judgment("uncertain", "evidence-missing")
        for quote, name in (
            (source_quote, "object_source"),
            (target_quote, "object_target"),
        ):
            word = claim.get(name)
            if isinstance(word, str) and word and word not in quote:
                return Judgment("uncertain", "evidence-missing")
        if verdict == "equivalent" and claim.get("same") is False:
            return Judgment("contradict", "binding")
        clash = _span_conflict(source_quote, target_quote)
        if clash and verdict == "equivalent":
            return Judgment("contradict", clash)
        source_quotes.append(source_quote)
        target_quotes.append(target_quote)
    if verdict == "conflict":
        return Judgment("contradict", "conflict")
    if not _covers(source, source_quotes) or not _covers(target, target_quotes):
        return Judgment("uncertain", "evidence-incomplete")
    return Judgment("accept", "equivalent")


def _parser_clears(source: str, target: str) -> bool:
    """只有一笔硬事实、而且全量检查已经通过时，没有第二笔对象可对调。"""
    source, target = _fold(source), _fold(target)
    if leaf_problem(source, target) is not None:
        return False
    hard = [fact for fact in _extract(target) if fact.hard and fact.kind in _COVER]
    return len(hard) < 2


def clues(source: str, target: str) -> str:
    def one(html: str) -> str:
        bits = [f"{fact.kind}:{fact.key}" for fact in _extract(_fold(html))]
        return " ".join(bits[:16])

    return f"{one(source)} => {one(target)}"


def verify_facts(source: str, target: str, data_dir: Path) -> tuple[Judgment, dict]:
    """另一次调用。生成稿不能自己说正确；返回的片段还要能在两侧原文里找到。"""
    import researchlib.translate.service as translate_service

    payload = {
        "verify": True,
        "evidence_id": evidence_id(source, target),
        "source": source,
        "target": target,
        "clues": clues(source, target),
    }
    result = translate_service.chat_json(
        system=_VERIFY_SYSTEM,
        user=payload,
        purpose="translate_verify",
        subject="slot",
        temperature=0.3,
        max_tokens=min(output_tokens(), 8000),
        data_dir=data_dir,
    )
    data = result.data if isinstance(result.data, dict) else {}
    return apply_report(source, target, data), data


def settle_numbers(
    source: str,
    draft: str,
    data_dir: Path,
    *,
    segment_id: str | None = None,
    source_hash: str | None = None,
) -> tuple[str, str | None, list[dict]]:
    """解析器只在单笔且已经通过时放行。其余做独立核验；没通过只修一次再核验。"""
    if _parser_clears(source, draft):
        return draft, None, [{
            "step": "check", "input": source, "output": draft,
            "status": "accept", "reason": "single",
        }]
    try:
        first, raw = verify_facts(source, draft, data_dir)
    except LLMTransportError:
        raise
    stages = [{
        "step": "verify", "input": source, "output": draft,
        "status": first.status, "reason": first.reason, "report": raw,
    }]
    if first.status == "accept":
        return draft, None, stages
    try:
        revised, identity, rejected = repair_slot(
            source, draft, data_dir, segment_id=segment_id, source_hash=source_hash,
        )
    except LLMTransportError:
        raise
    if identity:
        stages.append({
            "step": "repair", "input": source, "output": draft,
            "rejected": rejected, "status": "discarded", "reason": identity,
        })
        return draft, _code("uncertain"), stages
    if _parser_clears(source, revised):
        stages.append({
            "step": "repair", "input": source, "output": revised,
            "status": "accept", "reason": "single",
        })
        return revised, None, stages
    try:
        second, raw_second = verify_facts(source, revised, data_dir)
    except LLMTransportError:
        raise
    stages.append({
        "step": "repair", "input": source, "output": revised, "reason": "recheck",
    })
    stages.append({
        "step": "verify", "input": source, "output": revised,
        "status": second.status, "reason": second.reason, "report": raw_second,
    })
    if second.status == "accept":
        return revised, None, stages
    reason = second.reason if second.status == "contradict" else "uncertain"
    return revised, _code(reason), stages


def settle_unshield(
    source: str,
    draft: str,
    shielded: Shielded,
    data_dir: Path,
    *,
    segment_id: str | None = None,
    source_hash: str | None = None,
) -> tuple[str | None, list[dict]]:
    """占位或链接编号丢了就只修这一段一次。两阶段的输入输出都留下。"""
    stages = [{"step": "generate", "input": source, "output": draft, "reason": ""}]
    restored = unshield(draft, shielded)
    if restored is not None:
        stages[-1]["reason"] = "restored"
        return restored, stages
    stages[-1]["reason"] = "placeholder or link id did not restore"
    try:
        revised, identity, rejected = repair_slot(
            source, draft, data_dir, note=_UNSHIELD_NOTE,
            segment_id=segment_id, source_hash=source_hash,
        )
    except LLMTransportError:
        raise
    restored = None if identity else unshield(revised, shielded)
    if identity:
        reason = identity
    elif restored is not None:
        reason = "restored"
    else:
        reason = "repair still did not restore"
    stages.append({
        "step": "repair",
        "input": source,
        "output": revised,
        "rejected": rejected,
        "reason": reason,
    })
    return restored, stages

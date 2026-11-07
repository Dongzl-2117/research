"""译制闭环：结构完整性和全部数字事实分开判。

结构（占位、链接、图片、代码、条数）不对就只修这一段一次。
数字先做写法折叠（数字和中文单位之间的空格），再交给原来的全量检查。
钟点、日期、月份、财季、时段、比例、金额都参与，不能因为没单独收成证据就放过。
检查通过才留下。没通过只修一次，再用同一条检查复核；模型不能自己宣布通过。
没入库的每一阶段输入、输出和原因都留下，不含密钥。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from researchlib.translate.llm import LLMTransportError, output_tokens
from researchlib.translate.numbers import _extract
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


def settle_numbers(
    source: str,
    draft: str,
    data_dir: Path,
    *,
    segment_id: str | None = None,
    source_hash: str | None = None,
) -> tuple[str, str | None, list[dict]]:
    """证据一致就留下。否则只修一次，再用证据判，不采信模型的自我声明。"""
    first = judge_numbers(source, draft)
    stages = [{
        "step": "generate", "input": source, "output": draft,
        "status": first.status, "reason": first.reason,
    }]
    if first.status == "accept":
        return draft, None, stages
    try:
        revised, identity, rejected = repair_slot(
            source, draft, data_dir, segment_id=segment_id, source_hash=source_hash,
        )
    except LLMTransportError:
        raise
    second = judge_numbers(source, revised)
    stages.append({
        "step": "repair", "input": source, "output": revised,
        "rejected": rejected, "status": second.status,
        "reason": identity or second.reason,
    })
    if not identity and second.status == "accept":
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

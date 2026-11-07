"""译制闭环：结构完整性和数字事实分开判。

结构（占位、链接、图片、代码、条数）不对就只修这一段一次。
数字先收成证据：时段、区间、年代、比例、金额。证据对得上就留下，不改写去迎合抽取格式。
证据互相矛盾就拒绝。证据不够就标不确定，不把模型的一句「可以」当成通过。
修一次之后用同一套证据再判。没入库的每一阶段输入、输出和原因都留下，不含密钥。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from researchlib.translate.llm import LLMTransportError, output_tokens
from researchlib.translate.numbers import (
    _RATIO_NOISE,
    _decimal,
    _duration_count,
    _duration_unit,
    _extract,
    _extract_located,
    _qty_key,
)
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.provenance import numeric_anchors
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
_EN_RANGE = re.compile(
    r"\b(?:from\s+)?(?P<a>[a-z]+|\d+)\s+(?:to|through)\s+(?P<b>[a-z]+|\d+)\s+"
    r"(?P<u>weeks?|months?|days?|hours?)\b",
    re.I,
)
_ZH_RANGE = re.compile(
    r"(?P<a>\d+|[一二三四五六七八九十两]+)\s*(?:至|到)\s*"
    r"(?P<b>\d+|[一二三四五六七八九十两]+)\s*(?P<u>天|周|小时|个月|星期)"
)
_EN_DECADE = re.compile(r"\b(?:late|early|mid|middle)\s+(?P<yy>\d{2})s\b", re.I)
_ZH_DECADE = re.compile(r"(?P<c>[12]\d)\s*世纪\s*(?P<yy>\d{2})\s*年代")
_ZH_RATIO = re.compile(
    r"每(?!月|年|周|天|日|小|时|分|刻|位|户|个|名|人|次|股|份|家|篇|订|季)"
    r"(?P<head>[^0-9。\n]{0,16}?)"
    r"(?P<den>\d+(?:\.\d+)?)\s*美元"
    r"(?P<gap>[^。\n]{0,40}?)"
    r"(?:约|大约)?\s*(?P<num>\d+(?:\.\d+)?)\s*美元"
)
_AMOUNT_KINDS = {"qty", "literal"}


@dataclass
class Judgment:
    status: str
    reason: str


@dataclass
class Evidence:
    ranges: Counter = field(default_factory=Counter)
    decades: Counter = field(default_factory=Counter)
    ratios: Counter = field(default_factory=Counter)
    durations: Counter = field(default_factory=Counter)
    amounts: list = field(default_factory=list)
    literals: list = field(default_factory=list)
    soft: Counter = field(default_factory=Counter)


def _fold(html: str) -> str:
    return _UNIT_GAP.sub("", html or "")


def _money(raw: str) -> str | None:
    amount = _decimal((raw or "").replace(",", ""))
    if amount is None:
        return None
    return _qty_key(amount)


def _decade_year(year_two: str, century: str | None = None) -> str:
    yy = int(year_two)
    if century is not None:
        return str((int(century) - 1) * 100 + yy)
    return str(1900 + yy if yy >= 30 else 2000 + yy)


def _take(counter: Counter, key: str) -> None:
    if counter[key] > 0:
        counter[key] -= 1


def _evidence(html: str) -> Evidence:
    """把一段里的数字收成可对照的证据。写法不同，收成同一种对象。"""
    bag = Evidence()
    text, _located = _extract_located(html)
    used: Counter[str] = Counter()
    for pattern in (_EN_RANGE, _ZH_RANGE):
        for match in pattern.finditer(text):
            left, right = _duration_count(match.group("a")), _duration_count(match.group("b"))
            unit = _duration_unit(match.group("u"))
            if not left or not right or not unit or left in {"~2", "some"} or right in {"~2", "some"}:
                continue
            bag.ranges[(unit, frozenset((left, right)))] += 1
            used[left] += 1
            used[right] += 1
    for match in _EN_DECADE.finditer(text):
        bag.decades[_decade_year(match.group("yy"))] += 1
        used[str(int(match.group("yy")))] += 1
    for match in _ZH_DECADE.finditer(text):
        bag.decades[_decade_year(match.group("yy"), match.group("c"))] += 1
        used[str(int(match.group("c")))] += 1
        used[str(int(match.group("yy")))] += 1
    facts = _extract(html)
    ratios = [tuple(fact.key.split("/")) for fact in facts if fact.kind == "ratio" and "/" in fact.key]
    if not ratios:
        for match in _ZH_RATIO.finditer(text):
            if _RATIO_NOISE.search(match.group("head") + match.group("gap")):
                continue
            den, num = _money(match.group("den")), _money(match.group("num"))
            if den and num:
                ratios.append((num, den))
                break
    for num, den in ratios:
        bag.ratios[(num, den)] += 1
        used[num] += 1
        used[den] += 1
    range_units = {unit for (unit, _ends), count in bag.ranges.items() for _n in range(count)}
    range_counts = Counter(end for (_unit, ends), count in bag.ranges.items() for end in ends for _n in range(count))
    for fact in facts:
        if fact.kind == "duration" and ":" in fact.key:
            unit, count = fact.key.split(":", 1)
            if count in {"~2", "some", "couple"}:
                bag.durations[(unit, count)] += 1
                continue
            if unit in range_units and range_counts[count] > 0:
                range_counts[count] -= 1
                continue
            bag.durations[(unit, count)] += 1
            continue
        if fact.kind in {"ratio", "month", "date", "fiscal", "clock"}:
            continue
        if used[fact.key] > 0 and fact.kind in {"qty", "literal", "soft", "pct", "year"}:
            used[fact.key] -= 1
            continue
        if fact.kind == "qty" and fact.hard:
            bag.amounts.append(fact.key)
        elif fact.hard and fact.kind in {"literal", "pct", "year"}:
            bag.literals.append(fact.key)
        elif not fact.hard:
            bag.soft[fact.key] += 1
    return bag


def _gap_breaks(text: str, start: int, end: int) -> bool:
    if end < start:
        start, end = end, start
    return re.search(r"[.。!！?？;；\n]", text[start:end]) is not None


def _roles(html: str) -> tuple[str, str] | None:
    """一句里唯一的 on / 对 把两个数分成主题和对象。看不出就不猜。"""
    text, chosen = _extract_located(html)
    amounts = [item for item in chosen if item[0].hard and item[0].kind in _AMOUNT_KINDS]
    ons = list(re.finditer(r"\bon\b", text, re.I))
    duis = list(re.finditer(r"对(?=\s*\d)", text))
    if len(ons) + len(duis) != 1:
        return None

    def nearest(edge: int, *, before: bool):
        found = None
        for fact, start, end in amounts:
            if before:
                if end > edge or _gap_breaks(text, end, edge):
                    continue
                if found is None or end > found[2]:
                    found = (fact, start, end)
            elif start >= edge and not _gap_breaks(text, edge, start) and (found is None or start < found[1]):
                found = (fact, start, end)
        return found

    if ons:
        theme, ground = nearest(ons[0].start(), before=True), nearest(ons[0].end(), before=False)
        if theme is None or ground is None or theme[0].key == ground[0].key:
            return None
        return theme[0].key, ground[0].key
    after = []
    edge = duis[0].end()
    for item in amounts:
        if item[1] < edge or _gap_breaks(text, edge, item[1]):
            continue
        if after and _gap_breaks(text, after[-1][2], item[1]):
            break
        after.append(item)
        if len(after) == 2:
            break
    if len(after) < 2 or after[0][0].key == after[1][0].key:
        return None
    return after[1][0].key, after[0][0].key


def _literals_match(source: Evidence, target: Evidence) -> bool:
    soft = source.soft.copy()
    index = 0
    for key in target.literals:
        if index < len(source.literals) and source.literals[index] == key:
            index += 1
            continue
        if soft[key] > 0:
            soft[key] -= 1
            continue
        return False
    return index == len(source.literals)


def _durations_match(source: Evidence, target: Evidence) -> str | None:
    missing = source.durations - target.durations
    extra = target.durations - source.durations
    if missing:
        return "conflict"
    spare = source.soft["1"]
    known = {unit for unit, _count in source.durations}
    for (unit, count), count_n in extra.items():
        if count != "1" or unit not in known or spare < count_n:
            return "conflict"
        spare -= count_n
    return None


def judge_numbers(source: str, target: str) -> Judgment:
    """accept 证据一致；contradict 证据冲突；uncertain 证据不够，不能当成通过。"""
    source, target = _fold(source), _fold(target)
    left, right = _roles(source), _roles(target)
    if left and right and left != right:
        return Judgment("contradict", "binding")
    src, tgt = _evidence(source), _evidence(target)
    if src.ranges != tgt.ranges or src.decades != tgt.decades:
        return Judgment("contradict", "conflict")
    if src.ratios != tgt.ratios:
        return Judgment("contradict", "binding")
    duration = _durations_match(src, tgt)
    if duration:
        return Judgment("contradict", duration)
    if src.amounts != tgt.amounts:
        if sorted(src.amounts) == sorted(tgt.amounts) and src.amounts:
            # 只有一侧标出了论证角色时，先后不同还不能断定对调了对象。
            if bool(left) != bool(right):
                return Judgment("uncertain", "uncertain")
            return Judgment("contradict", "binding")
        anchors = numeric_anchors(source)
        if anchors and anchors == numeric_anchors(target):
            return Judgment("contradict", "scale")
        return Judgment("contradict", "conflict")
    if not _literals_match(src, tgt):
        return Judgment("contradict", "conflict")
    return Judgment("accept", "ok")


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


def _html_of(item: object, segment_id: str | None) -> str | None:
    if isinstance(item, str) and item.strip():
        return item
    if not isinstance(item, dict) or not isinstance(item.get("html"), str) or not item["html"].strip():
        return None
    if segment_id and item.get("id") not in {None, segment_id}:
        return None
    return item["html"]


def repair_slot(
    source: str,
    draft: str,
    data_dir: Path,
    *,
    note: str = _REPAIR_NOTE,
    segment_id: str | None = None,
    source_hash: str | None = None,
) -> str:
    """再要一稿。返回的不是通过证明；调用方还要再判一次。

    和生成一样传 temperature 0.3。DeepSeek 思考模式的请求不带这个字段。
    带了段身份时按正式契约回显，没有身份时仍接受字符串。
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
        return draft
    html = _html_of(raw[0], segment_id)
    return html or draft


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
        revised = repair_slot(
            source, draft, data_dir, segment_id=segment_id, source_hash=source_hash,
        )
    except LLMTransportError:
        raise
    second = judge_numbers(source, revised)
    stages.append({
        "step": "repair", "input": source, "output": revised,
        "status": second.status, "reason": second.reason,
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
        revised = repair_slot(
            source, draft, data_dir, note=_UNSHIELD_NOTE,
            segment_id=segment_id, source_hash=source_hash,
        )
    except LLMTransportError:
        raise
    restored = unshield(revised, shielded)
    stages.append({
        "step": "repair",
        "input": source,
        "output": revised,
        "reason": "restored" if restored is not None else "repair still did not restore",
    })
    return restored, stages

"""译制闭环里，一段对不齐时怎么处理。

段还是一段：条数、空段、链接、图片、代码不对就拒绝。括号和空格这类写法不单独挡。

数字按对象核对，不按集合。收入 1000 万、成本 2000 万不能换成收入 2000 万、成本 1000 万。
``on`` / ``对`` 两边的两个数可以换个说法的先后，角色本身不能换。
模型不能自己宣布通过。修一次仍由这里决定，不覆盖已经写好的译文。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from researchlib.translate.llm import LLMTransportError, output_tokens
from researchlib.translate.numbers import _align, _extract, _extract_located
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.provenance import leaf_problem, numeric_anchors
from researchlib.translate.segments import Shielded, unshield

logger = logging.getLogger("researchlib.translate")

_REPAIR_NOTE = (
    "这一段的数字和原文不是同一批事实。不要自己宣布通过。"
    "只改单位、数量级，或数字跟错了对象的地方。写法不用改。"
    "若事实已经一致，把译文原样放回 t。只输出 JSON，t 里只能有这一段。"
)
_UNSHIELD_NOTE = (
    "这一段的图片、代码占位或链接编号丢了。不要自己宣布通过。"
    "⟦n⟧、[[n]]、【n】 和 <a id=\"Ln\"> 按原文各留一次，编号不能改。"
    "只输出 JSON，t 里只能有这一段。"
)
# 中文单位可以紧贴数字。中间那个空格是写法，不是另一个数。
_UNIT_GAP = re.compile(r"(?<=\d)\s+(?=个月|星期|小时|天|周)")
_ROLE_GAP = 48
_AMOUNT_KINDS = {"qty", "literal"}


def _fold(html: str) -> str:
    return _UNIT_GAP.sub("", html or "")


def _gap_breaks(text: str, start: int, end: int) -> bool:
    if end < start:
        start, end = end, start
    return re.search(r"[.。!！?？;；\n]", text[start:end]) is not None


def _amounts(chosen: list[tuple]) -> list[tuple]:
    return [item for item in chosen if item[0].hard and item[0].kind in _AMOUNT_KINDS]


def _closest(text: str, amounts: list[tuple], edge: int, *, before: bool):
    found = None
    for fact, start, end in amounts:
        if before:
            if end > edge or edge - end > _ROLE_GAP or _gap_breaks(text, end, edge):
                continue
            if found is None or end > found[2]:
                found = (fact, start, end)
        elif start >= edge and start - edge <= _ROLE_GAP and not _gap_breaks(text, edge, start):
            if found is None or start < found[1]:
                found = (fact, start, end)
    return found


def _roles(html: str) -> tuple[str, str] | None:
    """``N on M`` 和 ``对 M … N`` 的（主题, 对象）。看不出这一对就不猜。"""
    text, chosen = _extract_located(html)
    amounts = _amounts(chosen)
    ons = list(re.finditer(r"\bon\b", text, re.I))
    duis = list(re.finditer(r"对(?=\s*\d)", text))
    if len(ons) + len(duis) != 1:
        return None
    if ons:
        marker = ons[0]
        theme = _closest(text, amounts, marker.start(), before=True)
        ground = _closest(text, amounts, marker.end(), before=False)
        if theme is None or ground is None or theme[0].key == ground[0].key:
            return None
        return theme[0].key, ground[0].key
    marker = duis[0]
    after = [
        item for item in amounts
        if item[1] >= marker.end()
        and item[1] - marker.end() <= _ROLE_GAP
        and not _gap_breaks(text, marker.end(), item[1])
    ]
    if len(after) < 2 or after[1][1] - after[0][2] > _ROLE_GAP or _gap_breaks(text, after[0][2], after[1][1]):
        return None
    ground, theme = after[0], after[1]
    if theme[0].key == ground[0].key:
        return None
    return theme[0].key, ground[0].key


def _pin(facts: list, theme: str, ground: str) -> list:
    """把这一对角色放成先主题、后对象，其它事实留在原位。"""
    found: dict[str, int] = {}
    for index, fact in enumerate(facts):
        if fact.hard and fact.kind in _AMOUNT_KINDS and fact.key in {theme, ground} and fact.key not in found:
            found[fact.key] = index
    if theme not in found or ground not in found:
        return facts
    left, right = found[theme], found[ground]
    if left < right:
        return facts
    pinned = list(facts)
    pinned[left], pinned[right] = pinned[right], pinned[left]
    return pinned


def semantic_ok(source: str, target: str) -> bool:
    """写法可以不同。数字集合相同不够；角色对调了就不收。"""
    source, target = _fold(source), _fold(target)
    left, right = _roles(source), _roles(target)
    if left and right and left != right:
        return False
    if leaf_problem(source, target) is None:
        return True
    if leaf_problem(source, target) != "numbers" or not (left and right and left == right):
        return False
    return _align(_pin(_extract(source), *left), _pin(_extract(target), *left))


def _unit_clash(source: str, target: str) -> bool:
    source_units: dict[str, set[str]] = {}
    for fact in _extract(source):
        if fact.hard and fact.kind == "duration" and ":" in fact.key:
            unit, count = fact.key.split(":", 1)
            source_units.setdefault(count, set()).add(unit)
    for fact in _extract(target):
        if not (fact.hard and fact.kind == "duration" and ":" in fact.key):
            continue
        unit, count = fact.key.split(":", 1)
        units = source_units.get(count)
        if units and unit not in units:
            return True
    return False


def numeric_verdict(source: str, target: str) -> str:
    """``ok`` 才可以留下。``binding`` 是同一批数跟错了对象。"""
    folded_source, folded_target = _fold(source), _fold(target)
    if semantic_ok(folded_source, folded_target):
        return "ok"
    left, right = _roles(folded_source), _roles(folded_target)
    if left and right and left != right:
        return "binding"
    problem = leaf_problem(folded_source, folded_target)
    if problem not in {None, "numbers"}:
        return "conflict"
    source_qty = [fact.key for fact in _extract(folded_source) if fact.hard and fact.kind == "qty"]
    target_qty = [fact.key for fact in _extract(folded_target) if fact.hard and fact.kind == "qty"]
    if source_qty != target_qty and sorted(source_qty) == sorted(target_qty) and source_qty:
        return "binding"
    anchors = numeric_anchors(folded_source)
    same_anchors = bool(anchors) and anchors == numeric_anchors(folded_target)
    if same_anchors and source_qty != target_qty and (source_qty or target_qty):
        return "scale"
    if _unit_clash(folded_source, folded_target):
        return "conflict"
    supply: set[str] = set()
    for fact in _extract(folded_source):
        supply.add(fact.key)
        if fact.kind == "ratio" and "/" in fact.key:
            supply.update(part for part in fact.key.split("/") if part)
    if any(fact.hard and fact.kind in _AMOUNT_KINDS and fact.key not in supply for fact in _extract(folded_target)):
        return "conflict"
    return "doubt"


def fact_brief(source: str, target: str) -> str:
    """给日志和修复请求的事实列表。只留种类和键。"""

    def brief(html: str) -> str:
        bits = []
        for fact in _extract(html):
            mark = "" if fact.hard else "~"
            bits.append(f"{mark}{fact.kind}:{fact.key}")
            if len(bits) >= 12:
                break
        return " ".join(bits)

    return f"{brief(source)} => {brief(target)}"[:240]


def note_reject(
    data_dir: Path,
    *,
    article_id: int,
    slot: str,
    kind: str,
    source: str,
    draft: str,
) -> None:
    """未入库的失败槽写到数据目录，方便不重跑就看原文、译文和分类。不含密钥。"""
    directory = Path(data_dir) / "translate-rejects"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "article_id": article_id,
        "slot": slot,
        "kind": kind,
        "source": source,
        "draft": draft,
        "facts": fact_brief(source, draft),
    }
    with (directory / f"{article_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.warning("译制未入库 article=%s slot=%s kind=%s", article_id, slot, kind)


def repair_slot(source: str, draft: str, data_dir: Path, *, note: str = _REPAIR_NOTE) -> str:
    """再要一稿。模型输出不合格就留着原译文。

    调用和生成一样传 temperature 0.3。DeepSeek 思考模式不接收这个字段，请求里不会出现；
    这里不改模型、思考或输出预算。
    """
    user = {
        "segments": [source],
        "repair": note + "\n事实: " + fact_brief(source, draft) + "\n译文:\n" + draft[:4000],
    }
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
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str) or not raw[0].strip():
        return draft
    return raw[0]


def _code(verdict: str) -> str:
    if verdict == "scale":
        return "numbers-scale"
    if verdict == "binding":
        return "numbers-binding"
    if verdict == "conflict":
        return "numbers"
    return "numbers-doubt"


def settle_numbers(source: str, draft: str, data_dir: Path) -> tuple[str, str | None]:
    """事实已经一致就留下。否则只修一次，再用同一条标准决定。"""
    if semantic_ok(source, draft):
        return draft, None
    try:
        revised = repair_slot(source, draft, data_dir)
    except LLMTransportError:
        raise
    if semantic_ok(source, revised):
        return revised, None
    return revised, _code(numeric_verdict(source, revised))


def settle_unshield(source: str, draft: str, shielded: Shielded, data_dir: Path) -> str | None:
    """占位或链接编号丢了就只修这一段一次。修不好返回 None，不整篇重来。"""
    restored = unshield(draft, shielded)
    if restored is not None:
        return restored
    try:
        revised = repair_slot(source, draft, data_dir, note=_UNSHIELD_NOTE)
    except LLMTransportError:
        raise
    return unshield(revised, shielded)

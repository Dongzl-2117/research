"""译制闭环里数字这一关怎么处理。

段还是一段：条数、空段、链接、图片、代码不对就拒绝。标记和写法不必一样。

数字只要表达的是同一批事实就收下：写法、中文数字和出现顺序都可以不同。
单位、数量级，以及数字所属的对象、比例方向、起止范围不能错。
不拿检查器的键或先后顺序当最终标准，也不把所有数字丢进一个集合就放过
（800 个数据点对 1 亿人，不能换成 100 个点对 8 亿人）。

对不上时只把这一段的原文、译文和事实列表交回模型修一次。
模型不能自己宣布通过。修好后仍用这里的事实是否一致来决定，不覆盖已经写好的译文。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from researchlib.translate.llm import LLMTransportError, output_tokens
from researchlib.translate.numbers import (
    _SCALE_EXP,
    _decimal,
    _duration_count,
    _duration_unit,
    _extract,
    _qty_key,
)
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.provenance import leaf_problem, numeric_anchors

_REPAIR_NOTE = (
    "这一段的数字和原文不是同一批事实。不要自己宣布通过。"
    "只改单位、数量级，或数字跟错了对象、比例方向、起止范围的地方。"
    "写法、中文数字和出现顺序不用改。若事实已经一致，把译文原样放回 t。"
    "只输出 JSON，t 里只能有这一段。"
)
_EN_RANGE = re.compile(
    r"\b(?:from\s+)?(?P<a>[a-z]+|\d+)\s+(?:to|through)\s+(?P<b>[a-z]+|\d+)\s+"
    r"(?P<u>weeks?|months?|days?|hours?)\b",
    re.I,
)
_ZH_RANGE = re.compile(
    r"(?P<a>\d+|[一二三四五六七八九十两]+)\s*(?:至|到)\s*"
    r"(?P<b>\d+|[一二三四五六七八九十两]+)\s*(?P<u>天|周|小时|个月)"
)
_EN_DECADE = re.compile(r"\b(?:late|early|mid|middle|the)\s+(?P<yy>\d{2})s\b", re.I)
_ZH_DECADE = re.compile(r"(?P<c>[12]\d)\s*世纪\s*(?P<yy>\d{2})\s*年代")
_ZH_RATIO = re.compile(
    r"每(?!月|年|周|天|日|小)"
    r"[^0-9]{0,16}?(?P<den>\d+(?:\.\d+)?)\s*美元"
    r"[^。\n]{0,40}?(?P<num>\d+(?:\.\d+)?)\s*美元"
)
_EN_SPAN = re.compile(
    r"\bfrom\s+\$?\s*(?P<a>\d[\d,.]*)\s*(?P<sa>million|billion|thousand)?\s+"
    r"(?:to|-)\s+\$?\s*(?P<b>\d[\d,.]*)\s*(?P<sb>million|billion|thousand)?\b",
    re.I,
)
_ZH_SPAN = re.compile(
    r"从\s*(?P<a>\d[\d,.]*)\s*(?P<sa>万亿|亿|万)?(?:美元)?\s*(?:增|降)?(?:到|至)\s*"
    r"(?P<b>\d[\d,.]*)\s*(?P<sb>万亿|亿|万)?"
)
# 数字和单位之间有空格时，旧抽取会把「24 小时」看成裸的 24。这里仍把单位算上。
_ZH_UNIT_GAP = re.compile(r"(?P<n>\d+)\s+(?P<u>天|周|小时|个月)")


def _money(raw: str, scale: str | None) -> str | None:
    amount = _decimal((raw or "").replace(",", ""))
    if amount is None:
        return None
    if scale:
        exp = _SCALE_EXP.get(scale.lower())
        if exp is None:
            return None
        amount = amount.scaleb(exp)
    return _qty_key(amount)


def _decade_of(year_two: str, century: str | None = None) -> str:
    yy = int(year_two)
    if century is not None:
        return str((int(century) - 1) * 100 + yy)
    return str(1900 + yy if yy >= 30 else 2000 + yy)


def _count_word(raw: str) -> str | None:
    count = _duration_count(raw)
    if count is None or count in {"couple", "~", "some"}:
        return None
    return count


class _Bundle:
    def __init__(self) -> None:
        self.decades: Counter[str] = Counter()
        self.ranges: Counter[tuple[str, frozenset[str]]] = Counter()
        self.ratios: Counter[tuple[str, str]] = Counter()
        self.spans: Counter[tuple[str, str]] = Counter()
        self.durations: Counter[tuple[str, str]] = Counter()
        self.numbers: Counter[str] = Counter()
        self.hard_numbers: Counter[str] = Counter()


def _bundle(html: str) -> _Bundle:
    """数字事实，不看出现顺序。比例、起止和年代各自绑在一起，不拆进同一个集合。"""
    bag = _Bundle()
    absorbed: Counter[str] = Counter()
    for match in _EN_DECADE.finditer(html):
        bag.decades[_decade_of(match.group("yy"))] += 1
        absorbed[str(int(match.group("yy")))] += 1
    for match in _ZH_DECADE.finditer(html):
        bag.decades[_decade_of(match.group("yy"), match.group("c"))] += 1
        absorbed[str(int(match.group("c")))] += 1
        absorbed[str(int(match.group("yy")))] += 1
    for pattern in (_EN_RANGE, _ZH_RANGE):
        for match in pattern.finditer(html):
            left, right = _count_word(match.group("a")), _count_word(match.group("b"))
            unit = _duration_unit(match.group("u"))
            if left is None or right is None or unit is None:
                continue
            bag.ranges[(unit, frozenset((left, right)))] += 1
            absorbed[left] += 1
            absorbed[right] += 1
    for match in _ZH_RATIO.finditer(html):
        den, num = _money(match.group("den"), None), _money(match.group("num"), None)
        if den and num:
            bag.ratios[(num, den)] += 1
            absorbed[den] += 1
            absorbed[num] += 1
    for match in _EN_SPAN.finditer(html):
        left = _money(match.group("a"), match.group("sa"))
        right = _money(match.group("b"), match.group("sb"))
        if left and right:
            bag.spans[(left, right)] += 1
            absorbed[left] += 1
            absorbed[right] += 1
    for match in _ZH_SPAN.finditer(html):
        left = _money(match.group("a"), match.group("sa"))
        right = _money(match.group("b"), match.group("sb"))
        if left and right:
            bag.spans[(left, right)] += 1
            absorbed[left] += 1
            absorbed[right] += 1
    for match in _ZH_UNIT_GAP.finditer(html):
        unit = _duration_unit(match.group("u"))
        if unit is None:
            continue
        bag.durations[(unit, str(int(match.group("n"))))] += 1
        absorbed[str(int(match.group("n")))] += 1
    for fact in _extract(html):
        if fact.kind == "ratio" and "/" in fact.key:
            num, _, den = fact.key.partition("/")
            bag.ratios[(num, den)] += 1
            absorbed[num] += 1
            absorbed[den] += 1
            continue
        if fact.kind == "duration" and ":" in fact.key:
            unit, count = fact.key.split(":", 1)
            if count in {"couple", "~", "some"}:
                continue
            bag.durations[(unit, count)] += 1
            absorbed[count] += 1
            continue
        if fact.kind == "qty" or fact.kind in {"literal", "soft", "pct", "year"}:
            bag.numbers[fact.key] += 1
            if fact.hard and fact.kind != "soft":
                bag.hard_numbers[fact.key] += 1
    for key, count in absorbed.items():
        for pool in (bag.numbers, bag.hard_numbers):
            pool[key] = max(0, pool[key] - count)
            if pool[key] == 0:
                del pool[key]
    # 起止范围已经单独记下，不要再要求端点各算一段时长。
    for (unit, ends), count in list(bag.ranges.items()):
        for end in ends:
            have = bag.durations[(unit, end)]
            drop = min(have, count)
            bag.durations[(unit, end)] -= drop
            if bag.durations[(unit, end)] == 0:
                del bag.durations[(unit, end)]
    return bag


def _restore_spans(bag: _Bundle) -> None:
    for (start, end), count in bag.spans.items():
        bag.numbers[start] += count
        bag.numbers[end] += count
        bag.hard_numbers[start] += count
        bag.hard_numbers[end] += count
    bag.spans = Counter()


def _fill_durations(source: _Bundle, target: _Bundle) -> bool:
    """两边的时段都要在。译文多出来的「一周」，只在原文已经有「周」且还剩一个 1 时才认。"""
    if any(source.durations[key] > target.durations[key] for key in source.durations):
        return False
    spare = source.numbers.copy()
    for key, need in target.durations.items():
        have = source.durations[key]
        if have >= need:
            continue
        unit, count = key
        if any(got == unit for got, _count in source.durations) and spare[count] >= need - have:
            spare[count] -= need - have
            continue
        return False
    source.numbers = spare
    return True


def facts_agree(source: str, target: str) -> bool:
    """数字事实一致就行。写法、顺序可以不同；单位、量级和绑在一起的关系不能错。"""
    left, right = _bundle(source), _bundle(target)
    if left.spans and right.spans and left.spans != right.spans:
        return False
    if not (left.spans and right.spans):
        _restore_spans(left)
        _restore_spans(right)
    if not _fill_durations(left, right):
        return False
    supply = left.numbers.copy()
    needed = right.numbers.copy()
    for (_unit, count), count_n in left.durations.items():
        supply[count] += count_n
    for (_unit, count), count_n in right.durations.items():
        needed[count] += count_n
    return (
        left.decades == right.decades
        and left.ranges == right.ranges
        and left.ratios == right.ratios
        and _covers(supply, right.numbers)
        and _covers(needed, left.hard_numbers)
    )


def _covers(supply: Counter[str], need: Counter[str]) -> bool:
    return all(supply[key] >= count for key, count in need.items())


def _atoms(html: str, *, hard_only: bool) -> list[str]:
    """硬事实里的数。时段带上单位；比例拆成两个数。软事实只在供给一侧使用。"""
    found: list[str] = []
    for fact in _extract(html):
        if hard_only and not fact.hard:
            continue
        if fact.kind == "duration":
            unit, _, count = fact.key.partition(":")
            if count:
                found.append(f"{unit}:{count}")
                found.append(count)
            continue
        if fact.kind == "ratio" and "/" in fact.key:
            found.extend(part for part in fact.key.split("/") if part)
            continue
        if fact.kind in {"qty", "literal", "pct", "soft", "year"}:
            found.append(fact.key)
    return found


def _unit_clash(source: str, target: str) -> bool:
    """两边都写成了时段，个数相同、单位不同。裸数字对上时段不算已证明的单位错误。"""
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


def _missing_hard_number(source: str, target: str) -> bool:
    """译文的硬数字在原文的硬事实和软事实里都找不到。"""
    supply = _atoms(source, hard_only=False)
    for atom in _atoms(target, hard_only=True):
        if atom in supply:
            supply.remove(atom)
            continue
        if ":" in atom:
            count = atom.split(":", 1)[1]
            if count in supply:
                continue
        return True
    return False


def numeric_verdict(source: str, target: str) -> str:
    """``ok`` 表示数字事实一致，不要求键或顺序相同。其余才是冲突或存疑。"""
    if facts_agree(source, target) and leaf_problem(source, target) in {None, "numbers"}:
        return "ok"
    problem = leaf_problem(source, target)
    if problem is None:
        return "ok"
    if problem != "numbers":
        return "conflict"
    # 看得见的数字串一样，但带单位的金额对不上：10 billion 写成 10亿。
    anchors = numeric_anchors(source)
    if anchors and anchors == numeric_anchors(target):
        source_qty = [fact.key for fact in _extract(source) if fact.hard and fact.kind == "qty"]
        target_qty = [fact.key for fact in _extract(target) if fact.hard and fact.kind == "qty"]
        if source_qty != target_qty and (source_qty or target_qty):
            return "scale"
    if _unit_clash(source, target) or _missing_hard_number(source, target):
        return "conflict"
    return "doubt"


def fact_brief(source: str, target: str) -> str:
    """给日志和修复请求的事实列表。只留种类和键，不把整段正文再抄进错误码。"""

    def brief(html: str) -> str:
        bits = []
        for fact in _extract(html):
            mark = "" if fact.hard else "~"
            bits.append(f"{mark}{fact.kind}:{fact.key}")
            if len(bits) >= 12:
                break
        return " ".join(bits)

    return f"{brief(source)} => {brief(target)}"[:240]


def repair_slot(source: str, draft: str, data_dir: Path) -> str:
    """带着这一段的原文、译文和事实列表再要一稿。模型输出不合格就留着原译文，交给检查器。"""
    user = {
        "segments": [source],
        "repair": _REPAIR_NOTE + "\n事实: " + fact_brief(source, draft) + "\n译文:\n" + draft[:4000],
    }
    # 走 service 上的 chat_json，和第一次生成同一条入口，测试替换也覆盖这一次。
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


def settle_numbers(source: str, draft: str, data_dir: Path) -> tuple[str, str | None]:
    """返回 (留下的译文, 仍要拒绝的错误码)。事实一致就留下，不必再叫模型。"""
    if numeric_verdict(source, draft) == "ok":
        return draft, None
    try:
        revised = repair_slot(source, draft, data_dir)
    except LLMTransportError:
        raise
    if numeric_verdict(source, revised) == "ok":
        return revised, None
    verdict = numeric_verdict(source, revised)
    if verdict == "ok":
        return revised, None
    code = "numbers-scale" if verdict == "scale" else ("numbers" if verdict == "conflict" else "numbers-doubt")
    return revised, code

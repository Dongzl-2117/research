"""检索去重和问库引用共同使用的数字口径。仅归一明确写出的单位和涨跌。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

_TRANSLATE = str.maketrans("０１２３４５６７８９．％＋－−，", "0123456789.%+--,")
_SCALES = {
    "k": 1000, "thousand": 1000, "万": 10**4, "m": 10**6, "mn": 10**6,
    "million": 10**6, "亿": 10**8, "b": 10**9, "bn": 10**9,
    "billion": 10**9, "万亿": 10**12, "trillion": 10**12,
}
_UNITS = {
    "percent": "%", "百分比": "%", "基点": "bp", "bps": "bp", "basis points": "bp",
    "percentage points": "个百分点", "times": "倍", "x": "倍", "×": "倍",
    "$": "USD", "usd": "USD", "美元": "USD", "美金": "USD", "人民币": "CNY",
    "rmb": "CNY", "cny": "CNY", "元": "元", "港元": "HKD", "港币": "HKD", "hk$": "HKD",
}
_TOKEN = re.compile(
    r"(?<![\d.])(?P<currency>HK\$|\$|(?:人民币|港币|港元|美元|USD|CNY|RMB)\s*)?"
    r"(?P<number>[+-]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+))"
    r"(?:\s*(?:[多余个](?=万|亿))?(?P<scale>万亿|万|亿|(?:trillion|billion|million|thousand|bn|mn|[kmb])(?![a-z])))?"
    r"(?:\s*(?P<unit>个百分点|百分比|%|‰|倍|×|基点|美元|美金|人民币|港元|港币|元"
    r"|(?:percentage points|basis points|percent|times|bps?|x|usd|cny|rmb)(?![a-z])))?",
    re.I,
)
_DECLINE = re.compile(r"(?:下降|下滑|减少|下跌|降低|降幅|跌幅|fell(?: by)?|declined(?: by)?)\s*$", re.I)


@dataclass(frozen=True)
class Number:
    value: Decimal
    unit: str
    label: str


def numbers(text: str) -> Iterator[Number]:
    text = (text or "").translate(_TRANSLATE)
    for match in _TOKEN.finditer(text):
        literal = match["number"].replace(",", "")
        scale = (match["scale"] or "").lower()
        unit = (match["unit"] or (match["currency"] or "").strip()).lower()
        unit = _UNITS.get(unit, unit)
        amount = Decimal(literal) * _SCALES.get(scale, 1)
        if unit == "bp":
            amount, unit = amount / 100, "个百分点"
        elif unit == "‰":
            amount, unit = amount / 10, "%"
        if amount > 0 and _DECLINE.search(text[max(0, match.start() - 16):match.start()]):
            amount = -amount
        label = literal + scale + (match["unit"] or "")
        yield Number(amount, unit, label)


def numeric_signature(text: str) -> tuple:
    """保留顺序。数字相同、标点不同的段落可以合并，正负、单位和数字归属次序不同的分别保留。"""
    return tuple((number.value, number.unit) for number in numbers(text))

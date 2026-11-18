"""检索去重和问库引用共同使用的数字口径。仅归一明确写出的单位和涨跌。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

_TRANSLATE = str.maketrans("０１２３４５６７８９．％＋－−，＄￥", "0123456789.%+--,$¥")
_SCALES = {
    "k": 1000, "thousand": 1000, "万": 10**4, "m": 10**6, "mn": 10**6,
    "million": 10**6, "亿": 10**8, "b": 10**9, "bn": 10**9,
    "billion": 10**9, "万亿": 10**12, "trillion": 10**12,
}
_UNITS = {
    "percent": "%", "百分比": "%", "基点": "bp", "bps": "bp", "basis points": "bp",
    "percentage points": "个百分点", "times": "倍", "x": "倍", "×": "倍",
    "$": "USD", "usd": "USD", "美元": "USD", "美金": "USD", "人民币": "CNY",
    "rmb": "CNY", "cny": "CNY", "元": "元", "港元": "HKD", "港币": "HKD", "hk$": "HKD", "hkd": "HKD",
    "eur": "EUR", "€": "EUR", "欧元": "EUR", "gbp": "GBP", "£": "GBP", "英镑": "GBP",
    "jpy": "JPY", "日元": "JPY", "日圆": "JPY",
}
_TOKEN = re.compile(
    r"(?<![\d.])(?:(?P<currency_sign>[+-])?(?P<currency>HK\$|[$£€¥]|人民币|港币|港元|美元|欧元|英镑|日元|日圆"
    r"|USD|HKD|CNY|RMB|EUR|GBP|JPY)\s*)?"
    r"(?P<number>[+-]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+))"
    r"(?:\s*(?:[多余个](?=万|亿))?(?P<scale>万亿|万|亿|(?:trillion|billion|million|thousand|bn|mn|[kmb])(?![a-z])))?"
    r"(?:\s*(?P<unit>个百分点|百分比|%|‰|倍|×|基点|美元|美金|人民币|港元|港币|欧元|英镑|日元|日圆|元"
    r"|(?:percentage points|basis points|percent|times|bps?|x|usd|hkd|cny|rmb|eur|gbp|jpy)(?![a-z])))?",
    re.I,
)
_DECLINE = re.compile(r"(?:下降|下滑|减少|下跌|降低|降幅|跌幅|fell(?: by)?|declined(?: by)?)\s*$", re.I)
# OCR 和 Markdown 都用竖线分列，附注号也常常写成 (17)。
_NOTE_LABEL = re.compile(
    r"(?:附注|附註|注释|註釋|脚注|腳註|注|註)(?:号|號|编号|編號)?"
    r"|(?:foot)?notes?(?:\s+(?:no\.?|number|ref\.?))?", re.I,
)
_WRAPPED_CELL = re.compile(r"\s*[(（]\s*(\S(?:.*?\S)?)\s*[)）]\s*")


def _accounting_cells(text: str) -> dict[int, int]:
    """记录整格括号内容的位置；数字解析器还须恰好读完整格。注明是附注号的行、列除外。"""
    cells = {}
    if "|" not in text or ("(" not in text and "（" not in text):
        return cells
    note_columns: set[int] = set()
    for row in re.finditer(r"^.*$", text, re.M):
        columns = row[0].split("|")
        if len(columns) == 1:
            note_columns.clear()
            continue
        if not columns[-1].strip():
            columns.pop()
        names = [c.strip() for c in columns]
        notes = {i for i, name in enumerate(names) if _NOTE_LABEL.fullmatch(name)}
        if notes and not any(_WRAPPED_CELL.fullmatch(name) for name in names):
            # 从右数，行首的可选装饰竖线不占数据列。
            note_columns = {i - len(columns) for i in notes}
        first = next((i for i, name in enumerate(names) if name), None)
        start = row.start()
        for i, column in enumerate(columns):
            wrapped = _WRAPPED_CELL.fullmatch(column)
            if wrapped and first not in notes and i - 1 not in notes and i - len(columns) not in note_columns:
                cells[start + wrapped.start(1)] = start + wrapped.end(1)
            start += len(column) + 1
    return cells


@dataclass(frozen=True)
class Number:
    value: Decimal
    unit: str
    label: str


def numbers(text: str) -> Iterator[Number]:
    text = (text or "").translate(_TRANSLATE)
    accounting = _accounting_cells(text)
    for match in _TOKEN.finditer(text):
        literal = match["number"].replace(",", "")
        scale = (match["scale"] or "").lower()
        unit = (match["unit"] or (match["currency"] or "").strip()).lower()
        unit = _UNITS.get(unit, unit)
        amount = Decimal(literal) * _SCALES.get(scale, 1)
        if match["currency_sign"] == "-":
            amount = -abs(amount)
        if unit == "bp":
            amount, unit = amount / 100, "个百分点"
        elif unit == "‰":
            amount, unit = amount / 10, "%"
        label = (match["currency_sign"] or "") + (match["currency"] or "") + literal + scale + (match["unit"] or "")
        if accounting.get(match.start()) == match.end():
            if not (match["currency_sign"] or literal.startswith(("+", "-"))):
                amount = -amount
            label = f"({label})"
        elif amount > 0 and _DECLINE.search(text[max(0, match.start() - 16):match.start()]):
            amount = -amount
        yield Number(amount, unit, label)


def numeric_signature(text: str) -> tuple:
    """保留顺序。数字相同、标点不同的段落可以合并，正负、单位和数字归属次序不同的分别保留。"""
    return tuple((number.value, number.unit) for number in numbers(text))

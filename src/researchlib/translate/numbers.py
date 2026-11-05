"""译段数字是否表达同一批事实。

改这里的等值规则时，把 ``provenance.ALIGNMENT_VERSION`` 加一。否则同一篇文章
会停在旧的拒绝记录上，不会用新规则再试。不要改 ``PIPELINE``，那会让已入库的
回执整篇变成不兼容。


写入时的 ``leaf_problem`` 原来把可见数字按字符顺序逐个比较。旧文档案里的合法中文
会因此整篇被拒：``March 29, 2013`` 写成 ``2013年3月29日`` 会多出一个月号并换序，
``$69.1 billion`` 写成 ``691亿美元``、``100%`` 写成 ``百分之百``、``Q3`` 写成
``第3季度`` 也过不了。模型每次都这样译，worker 就反复花钱。

这里只放行能对上的日期、财季、百分数和带单位的金额。数值变了、数字对调、版本号
少了末尾的零、把 ``'48`` 猜成 ``1948``，仍然不等。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

_MONTH_FULL = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTH_NUM = {name.lower(): index for index, name in enumerate(_MONTH_FULL, 1)}
_MONTH_NUM.update({name[:3].lower(): index for index, name in enumerate(_MONTH_FULL, 1)})
_MONTH_NUM["sept"] = 9
_MONTH_RE = "|".join(sorted(
    {*(_MONTH_NUM), "Sept"},
    key=len,
    reverse=True,
))
_SCALE_EXP = {"thousand": 3, "million": 6, "billion": 9, "trillion": 12, "万": 4, "亿": 8, "万亿": 12}
_CN_DIGIT = {**{ch: index for index, ch in enumerate("零一二三四五六七八九")}, "〇": 0}
_SPELLED = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
}
_SPELLED_RE = "|".join(sorted(_SPELLED, key=len, reverse=True))
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_ONES = "one|two|three|four|five|six|seven|eight|nine"
_AMOUNT = r"[-−+]?\d[\d,]*(?:\.\d+)?"
_SCALE = r"(?:trillion|billion|million|thousand)\b|万亿|亿|万"
_CURRENCY = r"(?:dollars?|euros?|pounds?|yen|美元|人民币|日元|欧元|英镑)\b"
_RANGE_DASH = r"[-–—~至到]"


@dataclass(frozen=True)
class Fact:
    kind: str
    key: str
    hard: bool = True


def same_numeric_facts(source: str, target: str) -> bool:
    """硬事实按顺序对上。月份名可以挪到句首；英文数词和脚注号可以不出现。"""
    return _align(_merge(_extract(source)), _merge(_extract(target)))


def _align(source: list[Fact], target: list[Fact]) -> bool:
    # 中文常把「in March」挪到金额前面。月份单独浮动，不能拿来解释一个普通数字。
    floaters = [fact for fact in source if fact.kind == "month"]
    seq = [fact for fact in source if fact.kind != "month"]
    if len(floaters) > 10:
        seq = [*seq, *(Fact("soft", fact.key, hard=False) for fact in floaters[10:])]
        floaters = floaters[:10]
    n, m = len(seq), len(target)
    masks = 1 << len(floaters)
    ok = [[[False] * masks for _ in range(m + 1)] for _ in range(n + 1)]
    ok[0][0][0] = True
    for i in range(n + 1):
        for j in range(m + 1):
            for mask in range(masks):
                if not ok[i][j][mask]:
                    continue
                if i < n and not seq[i].hard:
                    ok[i + 1][j][mask] = True
                if i < n and j < m and _compatible(seq[i], target[j]):
                    ok[i + 1][j + 1][mask] = True
                if j < m:
                    for index, floater in enumerate(floaters):
                        bit = 1 << index
                        if mask & bit or not _compatible(floater, target[j]):
                            continue
                        ok[i][j + 1][mask | bit] = True
    return any(ok[n][m])


def _compatible(left: Fact, right: Fact) -> bool:
    if left.kind == right.kind and left.key == right.key:
        return True
    if {left.kind, right.kind} <= {"qty", "literal", "soft"} and left.key == right.key:
        return True
    for fact, other in ((left, right), (right, left)):
        if (
            fact.kind == "fiscal"
            and re.fullmatch(r"Q[1-4]", fact.key)
            and other.kind in {"literal", "qty"}
            and other.key == fact.key[1:]
        ):
            return True
    return False


def _merge(facts: list[Fact]) -> list[Fact]:
    """紧挨着的「季 + 四位年」或「月日 + 四位年」收成一个事实，中英文语序可以不同。"""
    merged: list[Fact] = []
    for fact in facts:
        if merged and (combined := _combine(merged[-1], fact)) is not None:
            merged[-1] = combined
        else:
            merged.append(fact)
    return merged


def _combine(left: Fact, right: Fact) -> Fact | None:
    pairs = ((left, right), (right, left))
    for year_fact, other in pairs:
        year = _year(year_fact)
        if year is None:
            continue
        if other.kind == "date" and other.key.startswith("md:"):
            return Fact("date", f"d:{year}-{other.key.removeprefix('md:')}")
        quarter = _bare_quarter(other)
        if quarter:
            return Fact("fiscal", f"{year}Q{quarter}")
    return None


def _year(fact: Fact) -> str | None:
    if fact.kind == "literal" and re.fullmatch(r"(?:19|20)\d{2}", fact.key):
        return fact.key
    return None


def _bare_quarter(fact: Fact) -> str | None:
    if fact.kind == "fiscal" and re.fullmatch(r"Q[1-4]", fact.key):
        return fact.key[1]
    return None


class _Visible(HTMLParser):
    """代码里的数字不算；脚注上标里的纯数字是软事实，译文可以不重复那个圈号。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.code = 0
        self.sup = 0
        self.sup_parts: list[str] = []
        self.sup_link = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"pre", "code"}:
            self.code += 1
            self.parts.append(" ")
        if tag == "sup":
            if self.sup == 0:
                self.sup_parts = []
                self.sup_link = False
            self.sup += 1
        if tag == "a" and self.sup:
            self.sup_link = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"pre", "code"} and self.code:
            self.code -= 1
        if tag == "sup" and self.sup:
            self.sup -= 1
            if self.sup == 0:
                text = "".join(self.sup_parts).strip()
                if self.sup_link and text.isdigit():
                    self.parts.append(f" \u0001{text}\u0001 ")
                else:
                    self.parts.append(text)

    def handle_data(self, data: str) -> None:
        if self.code:
            return
        if self.sup:
            self.sup_parts.append(data)
        else:
            self.parts.append(data)


def _text(html: str) -> str:
    parser = _Visible()
    parser.feed(html or "")
    parser.close()
    return "".join(parser.parts)


def _extract(html: str) -> list[Fact]:
    text = _text(html)
    found: list[tuple[int, int, Fact]] = []
    for pattern, handler in _PATTERNS:
        for match in pattern.finditer(text):
            fact = handler(match)
            if fact is not None:
                found.append((match.start(), match.end(), fact))
    found.sort(key=lambda item: (item[0], item[0] - item[1]))
    chosen: list[Fact] = []
    cursor = 0
    for start, end, fact in found:
        if start < cursor:
            continue
        chosen.append(fact)
        cursor = end
    return chosen


def _qty_key(amount: Decimal) -> str:
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").replace("−", "-").removeprefix("+"))
    except Exception:
        return None


def _literal_key(raw: str) -> str:
    text = raw.replace("−", "-")
    if text.startswith("+"):
        text = text[1:]
    return text


def _valid(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _month_number(name: str) -> int | None:
    return _MONTH_NUM.get(name.lower())


def _keep_month(name: str, tail: str) -> bool:
    if name.lower() != "may":
        return _month_number(name) is not None
    # 「May be」是动词，不是五月。全小写的 may 同样不当月份。
    if not name[:1].isupper():
        return False
    return re.match(r"\s+be(?:ing|en)?\b", tail, re.I) is None


def _cn_small(word: str) -> int | None:
    if word in {"百", "一百"}:
        return 100
    if any(ch not in _CN_DIGIT and ch not in "十百" for ch in word):
        return None
    if "百" in word:
        head, _, tail = word.partition("百")
        hundreds = 1 if head in {"", "一"} else _CN_DIGIT.get(head or "", -1)
        if hundreds < 0:
            return None
        return hundreds * 100 + (_cn_small(tail) or 0 if tail else 0)
    if word == "十":
        return 10
    if "十" in word:
        tens, _, ones = word.partition("十")
        ten = 1 if tens == "" else _CN_DIGIT.get(tens, -1)
        one = 0 if ones == "" else _CN_DIGIT.get(ones, -1)
        if ten < 0 or one < 0:
            return None
        return ten * 10 + one
    if len(word) == 1 and word in _CN_DIGIT:
        return _CN_DIGIT[word]
    return None


def _quarter_number(token: str) -> str | None:
    token = token.strip()
    if token in {"1", "2", "3", "4", "一", "二", "三", "四"}:
        return str("1234一二三四".index(token) % 4 + 1) if token in "一二三四" else token
    words = {"first": "1", "second": "2", "third": "3", "fourth": "4", "1st": "1", "2nd": "2", "3rd": "3", "4th": "4"}
    return words.get(token.lower())


def _date_fact(year: int | None, month: int, day: int) -> Fact | None:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    if year is None:
        if not _valid(2024, month, day):
            return None
        return Fact("date", f"md:{month:02d}-{day:02d}")
    if not _valid(year, month, day):
        return None
    return Fact("date", f"d:{year:04d}-{month:02d}-{day:02d}")


def _range_fact(start: tuple[int, int, int], end: tuple[int, int, int]) -> Fact | None:
    if not _valid(*start) or not _valid(*end) or date(*start) > date(*end):
        return None
    left = f"{start[0]:04d}-{start[1]:02d}-{start[2]:02d}"
    right = f"{end[0]:04d}-{end[1]:02d}-{end[2]:02d}"
    return Fact("range", f"{left}..{right}")


def _int_group(match: re.Match[str], name: str) -> int:
    return int(match.group(name))


def _en_month(match: re.Match[str], group: str = "m") -> int | None:
    name = match.group(group)
    if not _keep_month(name, match.string[match.end(group):]):
        return None
    return _month_number(name)


def _on_cross_en(match: re.Match[str]) -> Fact | None:
    month1, month2 = _en_month(match, "m1"), _en_month(match, "m2")
    if month1 is None or month2 is None:
        return None
    year = _int_group(match, "y")
    return _range_fact((year, month1, _int_group(match, "d1")), (year, month2, _int_group(match, "d2")))


def _on_span_en(match: re.Match[str]) -> Fact | None:
    month = _en_month(match)
    if month is None:
        return None
    year = _int_group(match, "y")
    return _range_fact((year, month, _int_group(match, "d1")), (year, month, _int_group(match, "d2")))


def _on_full_en(match: re.Match[str]) -> Fact | None:
    month = _en_month(match)
    if month is None:
        return None
    return _date_fact(_int_group(match, "y"), month, _int_group(match, "d"))


def _on_month_year(match: re.Match[str]) -> Fact | None:
    month = _en_month(match)
    if month is None:
        return None
    return Fact("date", f"ym:{_int_group(match, 'y'):04d}-{month:02d}")


def _on_month_day(match: re.Match[str]) -> Fact | None:
    month = _en_month(match)
    if month is None:
        return None
    return _date_fact(None, month, _int_group(match, "d"))


def _on_bare_month(match: re.Match[str]) -> Fact | None:
    month = _en_month(match, "bare")
    if month is None:
        return None
    return Fact("month", str(month), hard=False)


def _on_cross_zh(match: re.Match[str]) -> Fact | None:
    year = _int_group(match, "y")
    return _range_fact(
        (year, _int_group(match, "m1"), _int_group(match, "d1")),
        (year, _int_group(match, "m2"), _int_group(match, "d2")),
    )


def _on_span_zh(match: re.Match[str]) -> Fact | None:
    year = _int_group(match, "y")
    month = _int_group(match, "m")
    return _range_fact((year, month, _int_group(match, "d1")), (year, month, _int_group(match, "d2")))


def _on_full_zh(match: re.Match[str]) -> Fact | None:
    return _date_fact(_int_group(match, "y"), _int_group(match, "m"), _int_group(match, "d"))


def _on_year_month(match: re.Match[str]) -> Fact | None:
    month = _int_group(match, "m")
    if not 1 <= month <= 12:
        return None
    return Fact("date", f"ym:{_int_group(match, 'y'):04d}-{month:02d}")


def _on_month_day_zh(match: re.Match[str]) -> Fact | None:
    return _date_fact(None, _int_group(match, "m"), _int_group(match, "d"))


def _on_month_only_zh(match: re.Match[str]) -> Fact | None:
    month = _int_group(match, "m")
    if not 1 <= month <= 12:
        return None
    return Fact("month", str(month))


def _on_iso(match: re.Match[str]) -> Fact | None:
    return _date_fact(_int_group(match, "y"), _int_group(match, "m"), _int_group(match, "d"))


def _on_fiscal(year: str | None, quarter: str) -> Fact | None:
    number = _quarter_number(quarter)
    if number is None:
        return None
    if year:
        return Fact("fiscal", f"{int(year):04d}Q{number}")
    return Fact("fiscal", f"Q{number}")


def _on_fy_q(match: re.Match[str]) -> Fact | None:
    return _on_fiscal(match.group("y"), match.group("q"))


def _on_q(match: re.Match[str]) -> Fact | None:
    return _on_fiscal(None, match.group("q"))


def _on_percent(match: re.Match[str]) -> Fact | None:
    amount = _decimal(match.group("n"))
    if amount is None:
        return None
    return Fact("pct", _qty_key(amount))


def _on_cn_percent(match: re.Match[str]) -> Fact | None:
    value = _cn_small(match.group("n"))
    if value is None:
        return None
    return Fact("pct", str(value))


def _on_qty(match: re.Match[str]) -> Fact | None:
    raw = match.group("amount")
    prefix, scale, currency = match.group("prefix"), match.group("scale"), match.group("currency")
    if not (prefix or scale or currency or (raw and "," in raw)):
        return None
    amount = _decimal(raw or "")
    if amount is None:
        return None
    if scale:
        amount = amount.scaleb(_SCALE_EXP[scale.lower()])
    return Fact("qty", _qty_key(amount))


def _on_literal(match: re.Match[str]) -> Fact | None:
    raw = match.group(0)
    if "," in raw or "%" in raw or "％" in raw:
        return None
    return Fact("literal", _literal_key(raw))


def _on_soft_marker(match: re.Match[str]) -> Fact | None:
    return Fact("soft", match.group(1), hard=False)


def _on_spelled(match: re.Match[str]) -> Fact | None:
    word = match.group(0).lower().replace("-", " ")
    if "-" in match.group(0):
        tens, ones = match.group(0).lower().split("-", 1)
        value = _SPELLED.get(tens, -1) + _SPELLED.get(ones, -1)
        if value < 20:
            return None
    else:
        value = _SPELLED.get(word)
        if value is None:
            return None
    return Fact("soft", str(value), hard=False)


_PATTERNS: list[tuple[re.Pattern[str], object]] = [
    (re.compile(
        rf"\b(?P<m1>{_MONTH_RE})\.?\s+(?P<d1>\d{{1,2}})\s*{_RANGE_DASH}\s*"
        rf"(?P<m2>{_MONTH_RE})\.?\s+(?P<d2>\d{{1,2}}),?\s*(?P<y>\d{{4}})\b",
        re.I,
    ), _on_cross_en),
    (re.compile(
        rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d1>\d{{1,2}})\s*{_RANGE_DASH}\s*(?P<d2>\d{{1,2}}),?\s*(?P<y>\d{{4}})\b",
        re.I,
    ), _on_span_en),
    (re.compile(
        rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b",
        re.I,
    ), _on_full_en),
    (re.compile(
        rf"(?P<y>\d{{4}})\s*年\s*(?P<m1>\d{{1,2}})\s*月\s*(?P<d1>\d{{1,2}})\s*[日号]?\s*{_RANGE_DASH}\s*"
        rf"(?P<m2>\d{{1,2}})\s*月\s*(?P<d2>\d{{1,2}})\s*[日号]?",
    ), _on_cross_zh),
    (re.compile(
        rf"(?P<y>\d{{4}})\s*年\s*(?P<m>\d{{1,2}})\s*月\s*(?P<d1>\d{{1,2}})\s*[日号]?\s*{_RANGE_DASH}\s*"
        rf"(?P<d2>\d{{1,2}})\s*[日号]?",
    ), _on_span_zh),
    (re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"), _on_full_zh),
    (re.compile(
        r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]\s*[，,]\s*(?P<y>\d{4})\s*年",
    ), _on_full_zh),
    (re.compile(rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<y>\d{{4}})\b", re.I), _on_month_year),
    (re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月"), _on_year_month),
    (re.compile(rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b", re.I), _on_month_day),
    (re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"), _on_month_day_zh),
    (re.compile(r"(?<!\d)(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})(?!\d)"), _on_iso),
    (re.compile(r"\bFY\s*(?P<y>\d{4})\s*Q(?P<q>[1-4])\b", re.I), _on_fy_q),
    (re.compile(r"\bQ(?P<q>[1-4])\s*FY\s*(?P<y>\d{4})\b", re.I), _on_fy_q),
    (re.compile(r"\bQ(?P<q>[1-4])\s+(?P<y>\d{4})\b", re.I), _on_fy_q),
    (re.compile(r"(?P<y>\d{4})\s*财年\s*第?\s*(?P<q>[1-4一二三四])\s*季度"), _on_fy_q),
    (re.compile(r"第?\s*(?P<q>[1-4一二三四])\s*季度\s*(?P<y>\d{4})\s*年"), _on_fy_q),
    (re.compile(
        r"\b(?P<q>1st|2nd|3rd|4th|first|second|third|fourth)\s+quarter(?:\s+of)?\s+(?P<y>\d{4})\b",
        re.I,
    ), _on_fy_q),
    (re.compile(r"\bQ(?P<q>[1-4])\b", re.I), _on_q),
    (re.compile(r"第?\s*(?P<q>[1-4一二三四])\s*季度"), _on_q),
    (re.compile(r"\b(?P<q>1st|2nd|3rd|4th|first|second|third|fourth)\s+quarter\b", re.I), _on_q),
    (re.compile(rf"(?<!\d)(?P<n>{_AMOUNT})\s*(?:%|％|percent\b)", re.I), _on_percent),
    (re.compile(
        r"百分之(?P<n>百|一百|[一二三四五六七八九]?十[一二三四五六七八九]?|[零〇一二三四五六七八九])"
        r"(?![零〇一二三四五六七八九十百千万亿])",
    ), _on_cn_percent),
    (re.compile(
        rf"(?:(?P<prefix>[$€£¥￥]|(?:USD|EUR|GBP|CNY|RMB|JPY)(?=\s*\d))\s*)?"
        rf"(?P<amount>{_AMOUNT})(?:\s*(?P<scale>{_SCALE}))?(?:\s*(?P<currency>{_CURRENCY}))?",
        re.I,
    ), _on_qty),
    (re.compile(r"(?P<m>\d{1,2})\s*月"), _on_month_only_zh),
    (re.compile(rf"\b(?P<bare>{_MONTH_RE})\b", re.I), _on_bare_month),
    (re.compile(rf"\b(?:{_TENS})-(?:{_ONES})\b", re.I), _on_spelled),
    (re.compile(rf"\b(?:{_SPELLED_RE})\b", re.I), _on_spelled),
    (re.compile(r"第(?P<ord>\d{1,4}|[一二三四五六七八九十百零〇]+)"), lambda match: (
        Fact("literal", str(int(match.group("ord")))) if match.group("ord").isdigit()
        else Fact("literal", str(value)) if (value := _cn_small(match.group("ord"))) is not None
        else None
    )),
    (re.compile(r"\u0001(\d+)\u0001"), _on_soft_marker),
    (re.compile(rf"(?<!\d){_AMOUNT}"), _on_literal),
]

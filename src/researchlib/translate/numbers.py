"""译段数字是否表达同一批事实。

改这里的等值规则时，把 ``provenance.ALIGNMENT_VERSION`` 加一。否则同一篇文章
会停在旧的拒绝记录上，不会用新规则再试。不要改 ``PIPELINE``，那会让已入库的
回执整篇变成不兼容。


写入时的 ``leaf_problem`` 原来把可见数字按字符顺序逐个比较。旧文档案里的合法中文
会因此整篇被拒：``March 29, 2013`` 写成 ``2013年3月29日`` 会多出一个月号并换序，
``$69.1 billion`` 写成 ``691亿美元``、``100%`` 写成 ``百分之百``、``Q3`` 写成
``第3季度`` 也过不了。模型每次都这样译，worker 就反复花钱。

这里只放行能对上的日期、财季、百分数和带单位的金额。2015–2017 的财报段落里，
模型还会把 ``$243 million`` 写成 ``2亿4300万``、把 ``Q2’16`` 写成 ``2016年第二季度``、
把 ``one billion`` 写成 ``10亿``，并把日期或百分数挪到金额前面。这些仍然是同一批事实。
``下一季度`` 不是第 1 季度，``第四财季`` 就是 Q4，``三年``、``两家``、``二十多岁`` 和
``10个中有6个`` 也是同一批数字。四位年份可以挪到金额前面。
``$220 billion, up from $200 billion`` 写成 ``从2000亿增至2200亿`` 仍是这一对，不能换成别的数。
数值变了、同一类数字对调、版本号少了末尾的零、把单独的 ``'48`` 猜成 ``1948``，仍然不等。
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
_SCALE_EXP = {
    "thousand": 3, "million": 6, "billion": 9, "trillion": 12,
    "k": 3, "m": 6, "bn": 9, "b": 9,
    "万亿": 12, "千亿": 11, "百亿": 10, "十亿": 9, "亿": 8,
    "千万": 7, "百万": 6, "十万": 5, "万": 4, "千": 3, "百": 2,
}
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
# 千分位必须成组。句尾逗号（``2026,``）不是数字的一部分，否则年份会被当成带逗号的数量。
_AMOUNT = r"[-−+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_SCALE = (
    r"(?:trillion|billion|million|thousand|bn)\b|[kmb]\b"
    r"|万亿|千亿|百亿|十亿|千万|百万|十万|亿|万|千|百"
)
_ORDERED_KINDS = ("date", "pct", "fiscal", "range", "half", "year", "clock", "duration")
_EN_SCALE = r"trillion|billion|million|thousand"
# 中文「美元」后面经常还是汉字。\b 只认在词字符和非词字符之间，元和后面的字都算词字符，边界对不上。
_CURRENCY = r"(?:dollars?|euros?|pounds?|yen)\b|美元|人民币|日元|欧元|英镑"
_RANGE_DASH = r"[-–—~至到]"


@dataclass(frozen=True)
class Fact:
    kind: str
    key: str
    hard: bool = True


def digits_hide_scale_change(source: str, target: str) -> bool:
    """可见数字串相同，并不表示数量级相同。

    ``$10-15 billion`` 和 ``10亿至15亿`` 的数字都是 10、15，但前者是 100亿到 150亿。
    译文如果根本没有硬数字（``one billion`` 写成 ``十亿``），不在这里判。
    """
    if same_numeric_facts(source, target):
        return False
    return any(fact.hard for fact in _extract(target))


def same_numeric_facts(source: str, target: str) -> bool:
    """硬事实按顺序对上。月份名可以挪到句首；日期、百分数、财季和四位年份可以挪到金额前后。

    英文数词和脚注号可以不出现。同一类事实（两个日期、两个百分数）不能互换。
    「到 Y、此前是 X」和「从 X 增至 Y」在原文写了 up from / to Y from X 时视为同一对。
    """
    if _facts_align(source, target):
        return True
    flipped = _flip_up_from_target(source, target)
    return flipped is not None and _facts_align(source, flipped)


def _facts_align(source: str, target: str) -> bool:
    return _align(_extract(source), _extract(target))


def _money_key(raw: str, scale: str | None) -> str | None:
    amount = _decimal(raw)
    if amount is None:
        return None
    unit = (scale or "").lower()
    if unit:
        exp = _SCALE_EXP.get(unit)
        if exp is None:
            return None
        amount = amount.scaleb(exp)
    return _qty_key(amount)


def _change_pairs(text: str) -> set[frozenset[str]]:
    """原文里「先写出结果、再写 up from / to Y from X」的那一对金额。"""
    found: set[frozenset[str]] = set()
    patterns = (
        re.compile(
            rf"\$?\s*(?P<y>{_AMOUNT})\s*(?P<ys>{_EN_SCALE})?\s*,?\s*(?:up|down)\s+from\s+"
            rf"[^.\n]{{0,48}}?\$?\s*(?P<x>{_AMOUNT})\s*(?P<xs>{_EN_SCALE})?",
            re.I,
        ),
        re.compile(
            rf"\bto\s+\$?\s*(?P<y>{_AMOUNT})\s*(?P<ys>{_EN_SCALE})?\s+from\s+"
            rf"[^.\n]{{0,48}}?\$?\s*(?P<x>{_AMOUNT})\s*(?P<xs>{_EN_SCALE})?",
            re.I,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            ys, xs = match.group("ys"), match.group("xs")
            later = _money_key(match.group("y"), ys or xs)
            earlier = _money_key(match.group("x"), xs or ys)
            if later and earlier and later != earlier:
                found.add(frozenset((later, earlier)))
    return found


_CN_CHANGE = re.compile(
    rf"从[^。；;\n\d]{{0,16}}?(?:约|大约|近|超过|逾|高于|低于|不足)?\s*"
    rf"(?P<xnum>{_AMOUNT})\s*(?P<xunit>万亿|千亿|百亿|十亿|亿|千万|百万|万)"
    rf"(?:美元|元|人民币)?"
    rf"(?P<mid>[^。；;\n]{{0,24}}?)"
    rf"(?:增至|上调至|提高到|提升至|增长至|增长到|扩大至|扩大到|下降至|下降到|降至|降到|到|至)\s*"
    rf"(?:约|大约|近|超过|逾|高于|低于)?\s*"
    rf"(?P<ynum>{_AMOUNT})\s*(?P<yunit>万亿|千亿|百亿|十亿|亿|千万|百万|万)"
)


def _flip_up_from_target(source: str, target: str) -> str | None:
    """把中文「从较早的数增至较晚的数」换成原文的表面顺序，只翻能对上的那一对。"""
    pairs = _change_pairs(_text(source))
    if not pairs:
        return None
    changed = False

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        earlier = _money_key(match.group("xnum"), match.group("xunit"))
        later = _money_key(match.group("ynum"), match.group("yunit"))
        if not earlier or not later or frozenset((earlier, later)) not in pairs:
            return match.group(0)
        whole = match.group(0)
        xnum, ynum = match.group("xnum"), match.group("ynum")
        xi = whole.find(xnum)
        yi = whole.find(ynum, xi + len(xnum))
        if xi < 0 or yi < 0:
            return whole
        changed = True
        return whole[:xi] + ynum + whole[xi + len(xnum):yi] + xnum + whole[yi + len(ynum):]

    flipped = _CN_CHANGE.sub(repl, target)
    if not changed:
        return None
    return flipped


def _align(source: list[Fact], target: list[Fact]) -> bool:
    # 中文常把「in March」挪到金额前面。月份单独浮动，不能拿来解释一个普通数字。
    # 日期、百分数、财季、上下半年和四位年份也常换到金额另一侧，但各自仍按原来的先后。
    floaters = [fact for fact in source if fact.kind == "month"]
    buckets = {kind: [] for kind in _ORDERED_KINDS}
    seq: list[Fact] = []
    for fact in source:
        if fact.kind == "month":
            continue
        bucket = buckets.get(fact.kind)
        if fact.hard and bucket is not None and len(bucket) < 8:
            bucket.append(fact)
        else:
            seq.append(fact)
    if len(floaters) > 10:
        seq.extend(Fact("soft", fact.key, hard=False) for fact in floaters[10:])
        floaters = floaters[:10]
    # 原文里出现过的 1–9。用来判断译文多出来的「两种 / 第一步」是在数已经有的东西，
    # 还是把 two 改成了三。
    small = {
        fact.key for fact in source
        if fact.key.isdigit() and 1 <= int(fact.key) <= 9
    }
    limits = tuple(len(buckets[kind]) for kind in _ORDERED_KINDS)
    start = (0, 0, (0,) * len(_ORDERED_KINDS), 0)
    seen = {start}
    stack = [start]

    def add(state: tuple) -> None:
        if state not in seen:
            seen.add(state)
            stack.append(state)

    visits = 0
    while stack:
        visits += 1
        if visits > 50_000:
            return False
        i, j, cursors, mask = stack.pop()
        if i == len(seq) and j == len(target) and cursors == limits:
            return True
        if i < len(seq) and not seq[i].hard:
            add((i + 1, j, cursors, mask))
        if i < len(seq) and j < len(target) and _compatible(seq[i], target[j]):
            add((i + 1, j + 1, cursors, mask))
        if j >= len(target):
            continue
        for index, kind in enumerate(_ORDERED_KINDS):
            cursor = cursors[index]
            if cursor >= limits[index]:
                continue
            if not _compatible(buckets[kind][cursor], target[j], bare_quarter=False):
                continue
            nxt = list(cursors)
            nxt[index] += 1
            add((i, j + 1, tuple(nxt), mask))
        for index, floater in enumerate(floaters):
            bit = 1 << index
            if mask & bit or not _compatible(floater, target[j], bare_quarter=False):
                continue
            add((i, j + 1, cursors, mask | bit))
        if _optional_count(target[j], small):
            add((i, j + 1, cursors, mask))
    return False


def _optional_count(fact: Fact, source_small: set[str]) -> bool:
    """并列项常被写成「两种」「第一步」，原文并没有这个数。

    只放过 1–4 的裸数字。原文已经有别的 1–9 时，不能拿一个新的个数换掉它。
    金额仍是 qty，``$2`` 写成别的数不会从这里漏过去。
    """
    if fact.kind != "literal" or fact.key not in {"1", "2", "3", "4"}:
        return False
    return not source_small or fact.key in source_small


def _same_amount(left: str, right: str) -> bool:
    """金额可以丢掉小数尾部的 0。版本号两边都是字面量，不走这里，``3.10`` 不等于 ``3.1``。"""
    a, b = _decimal(left), _decimal(right)
    return a is not None and b is not None and a == b


def _compatible(left: Fact, right: Fact, *, bare_quarter: bool = True) -> bool:
    if left.kind == right.kind and left.key == right.key:
        return True
    kinds = {left.kind, right.kind}
    if kinds <= {"qty", "literal", "soft"} and left.key == right.key:
        return True
    if "qty" in kinds and kinds <= {"qty", "literal", "soft"} and _same_amount(left.key, right.key):
        return True
    if (
        bare_quarter
        and left.kind == "fiscal"
        and re.fullmatch(r"Q[1-4]", left.key)
        and right.kind in {"literal", "qty", "soft"}
        and right.key == left.key[1:]
    ):
        return True
    return False


def _near(gap: str) -> bool:
    """只合并紧挨着的月日和年份、季度和年份。隔着句子的下一年份不是这一个季度的年份。"""
    return len(gap) <= 16 and re.search(r"[.。．!！?？;；]", gap) is None


def _merge_near(items: list[tuple[Fact, int, int]], text: str) -> list[Fact]:
    merged: list[tuple[Fact, int, int]] = []
    for fact, start, end in items:
        if merged and _near(text[merged[-1][2]:start]) and (combined := _combine(merged[-1][0], fact)) is not None:
            merged[-1] = (combined, merged[-1][1], end)
        else:
            merged.append((fact, start, end))
    return [fact for fact, _, _ in merged]


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
    if fact.kind == "year":
        return fact.key
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


def _mark_footnote(text: str) -> str:
    """句号后紧贴的脚注号不是金额。``million.1 Don’t``、``April 24.2 It’s`` 可以不译出。"""

    def marker(number: str) -> str:
        return f"\u0001{number}\u0001"

    text = re.sub(
        r"(?<=[A-Za-z])\.(?P<n>\d{1,2})(?=\s*(?:[A-Z]|$))",
        lambda match: marker(match.group("n")),
        text,
    )
    text = re.sub(
        rf"\b(?P<mon>{_MONTH_RE})\.?\s+(?P<day>\d{{1,2}})\.(?P<n>\d{{1,2}})(?=\s+[A-Z])",
        lambda match: f"{match.group('mon')} {match.group('day')}{marker(match.group('n'))}",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(?<=\d{4})\.(?P<n>\d{1,2})(?=\s+[A-Z])",
        lambda match: marker(match.group("n")),
        text,
    )
    return text


def _extract(html: str) -> list[Fact]:
    text = _mark_footnote(_text(html))
    found: list[tuple[int, int, int, Fact]] = []
    for pattern, handler in _PATTERNS:
        for match in pattern.finditer(text):
            emitted = handler(match)
            if emitted is None:
                continue
            facts = emitted if isinstance(emitted, list) else [emitted]
            for seq, fact in enumerate(facts):
                if fact is not None:
                    found.append((match.start(), match.end(), seq, fact))
    found.sort(key=lambda item: (item[0], item[0] - item[1], item[2]))
    chosen: list[tuple[Fact, int, int]] = []
    cursor = 0
    index = 0
    while index < len(found):
        start, end, _seq, fact = found[index]
        if start < cursor:
            index += 1
            continue
        chosen.append((fact, start, end))
        cursor = end
        index += 1
        while index < len(found) and found[index][0] == start and found[index][1] == end:
            chosen.append((found[index][3], start, end))
            index += 1
    return _merge_near(chosen, text)


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


def _expand_yy(raw: str) -> str | None:
    """财季缩写里的两位年。单独的 ``'48`` 不走这里，不把世纪猜出来。"""
    if re.fullmatch(r"\d{4}", raw):
        return f"{int(raw):04d}"
    if re.fullmatch(r"\d{2}", raw):
        year = int(raw)
        base = 2000 if year < 40 else 1900
        return f"{base + year:04d}"
    return None


def _half_fact(which: str, year: str | None) -> Fact:
    if year and (expanded := _expand_yy(year)):
        return Fact("half", f"{expanded}H{which}")
    return Fact("soft", f"H{which}", hard=False)


def _on_half_en(match: re.Match[str]) -> Fact:
    which = match.group("h")
    if not which.isdigit():
        which = "1" if which.lower() == "first" else "2"
    return _half_fact(which, match.groupdict().get("y"))


def _on_half_zh(match: re.Match[str]) -> Fact:
    which = "1" if match.group("w") == "上" else "2"
    return _half_fact(which, match.groupdict().get("y"))


def _on_q_short(match: re.Match[str]) -> Fact | None:
    quarter = match.group("q")
    year = match.groupdict().get("y")
    if year:
        expanded = _expand_yy(year)
        if expanded is None:
            return None
        return Fact("fiscal", f"{expanded}Q{quarter}")
    return Fact("fiscal", f"Q{quarter}")


def _on_fy_short(match: re.Match[str]) -> Fact | None:
    expanded = _expand_yy(match.group("y"))
    if expanded is None:
        return None
    return Fact("year", expanded)


def _signed(amount: Decimal, sign: str | None) -> Decimal:
    if sign:
        return -abs(amount)
    return amount


def _on_qty(match: re.Match[str]) -> Fact | None:
    raw = match.group("amount")
    prefix, scale, currency = match.group("prefix"), match.group("scale"), match.group("currency")
    sign = match.group("sign")
    if raw == "两":
        # 「两家」交给后面的量词；没有单位的「两」不是一笔金额。
        if not (prefix or scale or currency):
            return None
        amount: Decimal | None = Decimal(2)
    else:
        # 单独的负号不够：``2016-17`` 里的连字符不是负数。
        if not (prefix or scale or currency or (raw and "," in raw)):
            return None
        amount = _decimal(raw or "")
    if amount is None:
        return None
    if scale:
        key = scale.lower()
        if key in {"k", "m", "b"} and not prefix:
            return None
        amount = amount.scaleb(_SCALE_EXP[key])
    if sign in {"-", "−", "负"}:
        amount = _signed(amount, sign)
    return Fact("qty", _qty_key(amount))


def _on_cn_compound(match: re.Match[str]) -> Fact | None:
    """``2亿4300万`` 是一个数，不是 2亿再加一个无关的 4300万。"""
    head = Decimal(2) if match.group("a") == "两" else _decimal(match.group("a"))
    tail = _decimal(match.group("b"))
    if head is None or tail is None:
        return None
    total = head * Decimal(10) ** 8 + tail * Decimal(10) ** _SCALE_EXP[match.group("u")]
    total = _signed(total, match.group("sign"))
    return Fact("qty", _qty_key(total))


def _on_money_span(match: re.Match[str]) -> list[Fact] | None:
    scale = match.group("scale").lower()
    left, right = _decimal(match.group("a")), _decimal(match.group("b"))
    if left is None or right is None or scale not in _SCALE_EXP:
        return None
    exp = _SCALE_EXP[scale]
    return [Fact("qty", _qty_key(left.scaleb(exp))), Fact("qty", _qty_key(right.scaleb(exp)))]


def _on_percent_span(match: re.Match[str]) -> list[Fact] | None:
    left, right = _decimal(match.group("a")), _decimal(match.group("b"))
    if left is None or right is None:
        return None
    return [Fact("pct", _qty_key(left)), Fact("pct", _qty_key(right))]


def _on_clock(match: re.Match[str]) -> Fact | None:
    hour, minute = int(match.group("h")), int(match.group("mi"))
    if not (0 <= hour <= 24 and 0 <= minute <= 59):
        return None
    return Fact("clock", f"{hour}:{minute:02d}")


def _duration_key(raw: str) -> str | None:
    if raw.isdigit():
        return str(int(raw))
    if raw == "两":
        return "2"
    spelled = _SPELLED.get(raw.lower())
    if spelled is not None:
        return str(spelled)
    value = _cn_token(raw)
    if value is None:
        return None
    return str(value)


def _on_duration(match: re.Match[str]) -> Fact | None:
    key = _duration_key(match.group("n"))
    if key is None:
        return None
    return Fact("duration", key)


def _on_couple(_match: re.Match[str]) -> Fact:
    return Fact("duration", "2")


def _on_per(match: re.Match[str]) -> Fact | None:
    """``$0.80 for every $1`` 和 ``每1美元……0.80美元`` 是同一个比例，谁写在前面都行。"""
    part, unit = _decimal(match.group("a")), _decimal(match.group("b"))
    if part is None or unit is None:
        return None
    return Fact("ratio", f"{_qty_key(part)}/{_qty_key(unit)}")


def _on_cents(match: re.Match[str]) -> Fact | None:
    amount = _decimal(match.group("n"))
    if amount is None:
        return None
    return Fact("qty", _qty_key(amount / Decimal(100)))


def _spelled_head(head: str) -> Decimal | None:
    text = re.sub(r"\s+", " ", head.lower().replace("-", " ")).strip()
    if text in {"half", "half a"}:
        return Decimal("0.5")
    if text in {"a", "an"}:
        return Decimal(1)
    parts = text.split(" ")
    if len(parts) == 2 and parts[0] in _SPELLED and parts[1] in _SPELLED:
        total = _SPELLED[parts[0]] + _SPELLED[parts[1]]
        if total >= 20:
            return Decimal(total)
        return None
    value = _SPELLED.get(text)
    if value is None:
        return None
    return Decimal(value)


def _on_spelled_scale(match: re.Match[str]) -> Fact | None:
    head = _spelled_head(match.group("head"))
    if head is None:
        return None
    if match.group("hundred"):
        head *= 100
    scale = (match.group("scale") or match.group("scale2") or "").lower()
    if scale == "percent":
        if not match.group("hundred") and match.group("head").lower() in {"a", "an", "half", "half a"}:
            return None
        return Fact("pct", _qty_key(head))
    if scale:
        return Fact("qty", _qty_key(head.scaleb(_SCALE_EXP[scale])))
    if match.group("hundred"):
        return Fact("qty", _qty_key(head))
    return None


def _on_literal(match: re.Match[str]) -> Fact | None:
    raw = match.group(0)
    if "," in raw or "%" in raw or "％" in raw:
        return None
    key = _literal_key(raw)
    if re.fullmatch(r"(?:19|20)\d{2}", key):
        return Fact("year", key)
    return Fact("literal", key)


def _cn_token(word: str) -> int | None:
    if word == "两":
        return 2
    if word in {"两百", "二百"}:
        return 200
    return _cn_small(word)


def _on_cn_scale(match: re.Match[str]) -> Fact | None:
    """``十亿``、``二十亿``、``百万用户``。``千万不要`` 这种副词后面不是量词，不收。"""
    head = _cn_token(match.group("head"))
    scale = match.group("scale")
    if head is None or scale not in _SCALE_EXP:
        return None
    return Fact("qty", _qty_key(Decimal(head).scaleb(_SCALE_EXP[scale])))


def _on_cn_pair(match: re.Match[str]) -> list[Fact] | None:
    left, right = _cn_token(match.group("a")), _cn_token(match.group("b"))
    if left is None or right is None:
        return None
    return [Fact("literal", str(left)), Fact("literal", str(right))]


def _on_cn_measure(match: re.Match[str]) -> Fact | None:
    value = _cn_token(match.group("n"))
    if value is None:
        return None
    return Fact("literal", str(value))


def _on_year_span(match: re.Match[str]) -> list[Fact] | None:
    """``2016-17`` 是 2016 和 2017。单独的 ``'48`` 不走这里。"""
    start = int(match.group("y"))
    end = (start // 100) * 100 + int(match.group("yy"))
    if end < start:
        end += 100
    if not 1 <= end - start <= 30:
        return None
    return [Fact("year", f"{start:04d}"), Fact("year", f"{end:04d}")]


def _on_quarter_list(match: re.Match[str]) -> list[Fact] | None:
    facts = []
    for token in re.findall(r"第?\s*([1-4一二三四])", match.group(0)):
        number = _quarter_number(token)
        if number is None:
            return None
        facts.append(Fact("fiscal", f"Q{number}"))
    if len(facts) < 2:
        return None
    return facts


def _on_named_quarter(match: re.Match[str]) -> Fact | None:
    return _on_fiscal(match.groupdict().get("y"), match.group("q"))


def _on_out_of(match: re.Match[str]) -> list[Fact] | None:
    """``6 out of 10`` 和 ``10个中有6个`` 都是先部分、后整体。写反了就对不上。"""
    part, whole = _decimal(match.group("a")), _decimal(match.group("b"))
    if part is None or whole is None:
        return None
    return [Fact("literal", _qty_key(part)), Fact("literal", _qty_key(whole))]


def _on_both(_match: re.Match[str]) -> Fact:
    return Fact("soft", "2", hard=False)


def _on_decade(match: re.Match[str]) -> Fact:
    word = match.group(0).lower()
    return Fact("soft", "100" if word.startswith("centur") else "10", hard=False)


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


_ORD = r"(?:st|nd|rd|th)?"
_PATTERNS: list[tuple[re.Pattern[str], object]] = [
    (re.compile(
        rf"\b(?P<m1>{_MONTH_RE})\.?\s+(?P<d1>\d{{1,2}}){_ORD}\s+(?:to|through)\s+"
        rf"(?P<m2>{_MONTH_RE})\.?\s+(?P<d2>\d{{1,2}}){_ORD},?\s*(?P<y>\d{{4}})\b",
        re.I,
    ), _on_cross_en),
    (re.compile(
        rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d1>\d{{1,2}}){_ORD}\s+(?:to|through)\s+"
        rf"(?P<d2>\d{{1,2}}){_ORD},?\s*(?P<y>\d{{4}})\b",
        re.I,
    ), _on_span_en),
    (re.compile(
        rf"\b(?P<m1>{_MONTH_RE})\.?\s+(?P<d1>\d{{1,2}}){_ORD}\s*{_RANGE_DASH}\s*"
        rf"(?P<m2>{_MONTH_RE})\.?\s+(?P<d2>\d{{1,2}}){_ORD},?\s*(?P<y>\d{{4}})\b",
        re.I,
    ), _on_cross_en),
    (re.compile(
        rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d1>\d{{1,2}}){_ORD}\s*{_RANGE_DASH}\s*"
        rf"(?P<d2>\d{{1,2}}){_ORD},?\s*(?P<y>\d{{4}})\b",
        re.I,
    ), _on_span_en),
    (re.compile(
        rf"\b(?P<d>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<m>{_MONTH_RE})\.?,?\s+(?P<y>\d{{4}})\b",
        re.I,
    ), _on_full_en),
    (re.compile(
        rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}}){_ORD},?\s+(?P<y>\d{{4}})\b",
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
    (re.compile(rf"\b(?P<m>{_MONTH_RE})\.?,?\s+(?P<y>\d{{4}})\b", re.I), _on_month_year),
    (re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月"), _on_year_month),
    (re.compile(rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b", re.I), _on_month_day),
    (re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"), _on_month_day_zh),
    (re.compile(r"(?<!\d)(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})(?!\d)"), _on_iso),
    (re.compile(r"\bH(?P<h>[12])\s*['’]?\s*(?P<y>\d{2}|\d{4})\b", re.I), _on_half_en),
    (re.compile(r"\b(?P<h>[12])H\s*['’]?\s*(?P<y>\d{2}|\d{4})\b", re.I), _on_half_en),
    (re.compile(r"\b(?:the\s+)?(?P<h>first|second)\s+half\s+of\s+(?P<y>\d{4})\b", re.I), _on_half_en),
    (re.compile(r"(?P<y>\d{4})\s*年\s*(?P<w>上|下)半年"), _on_half_zh),
    (re.compile(r"\bH(?P<h>[12])\b", re.I), _on_half_en),
    (re.compile(r"\b(?P<h>[12])H\b", re.I), _on_half_en),
    (re.compile(r"(?<![上下年])(?P<w>上|下)半年"), _on_half_zh),
    (re.compile(r"\bFY\s*(?P<y>\d{4})\s*Q(?P<q>[1-4])\b", re.I), _on_fy_q),
    (re.compile(r"\bQ(?P<q>[1-4])\s*FY\s*(?P<y>\d{4})\b", re.I), _on_fy_q),
    (re.compile(r"\bQ(?P<q>[1-4])\s*['’](?P<y>\d{2})\b"), _on_q_short),
    (re.compile(r"\b(?P<q>[1-4])Q\s*FY\s*['’]?(?P<y>\d{2,4})\b", re.I), _on_q_short),
    (re.compile(r"\b(?P<q>[1-4])Q\s*['’]?(?P<y>\d{2,4})\b", re.I), _on_q_short),
    (re.compile(r"\bFQ(?P<q>[1-4])(?:\s*['’]?(?P<y>\d{2,4}))?\b", re.I), _on_q_short),
    (re.compile(r"\bQ(?P<q>[1-4])\s+(?P<y>\d{4})\b", re.I), _on_fy_q),
    (re.compile(r"\bFY\s*['’]?(?P<y>\d{2})\b", re.I), _on_fy_short),
    (re.compile(
        r"(?P<y>\d{4})\s*年\s*(?:第\s*)?(?<![上下前后这每])(?P<q>[1-4一二三四])\s*(?:季度|财季)",
    ), _on_fy_q),
    (re.compile(
        r"(?P<y>\d{4})\s*财年\s*(?:第\s*)?(?<![上下前后这每])(?P<q>[1-4一二三四])\s*(?:季度|财季)",
    ), _on_fy_q),
    (re.compile(
        r"(?<![上下前后这每])(?:第\s*)?(?P<q>[1-4一二三四])\s*(?:季度|财季)\s*(?P<y>\d{4})\s*年",
    ), _on_fy_q),
    (re.compile(
        r"(?:第?\s*[1-4一二三四]\s*[、，,和及与]\s*)+第?\s*[1-4一二三四]\s*(?:季度|财季)",
    ), _on_quarter_list),
    (re.compile(
        r"\b(?:fiscal\s+)?(?P<q>1st|2nd|3rd|4th|first|second|third|fourth)"
        r"(?:\s+fiscal)?[-\s]+quarters?(?:\s+of)?(?:\s+(?P<y>\d{4}))?\b",
        re.I,
    ), _on_named_quarter),
    (re.compile(r"\bQ(?P<q>[1-4])\b", re.I), _on_q),
    (re.compile(
        r"(?<![上下前后这每])(?:第\s*)?(?P<q>[1-4一二三四])\s*(?:季度|财季)",
    ), _on_q),
    (re.compile(
        rf"\bbetween\s+(?P<a>{_AMOUNT})\s+and\s+(?P<b>{_AMOUNT})\s*(?:%|％|percent\b)",
        re.I,
    ), _on_percent_span),
    (re.compile(
        rf"(?<!\d)(?P<a>{_AMOUNT})\s*(?:-|–|—|to)\s*(?P<b>{_AMOUNT})\s*(?:%|％|percent\b)",
        re.I,
    ), _on_percent_span),
    (re.compile(rf"\(\s*(?P<n>{_AMOUNT})\s*\)\s*(?:%|％|percent\b)", re.I), _on_percent),
    (re.compile(rf"(?<!\d)(?P<n>{_AMOUNT})\s*(?:%|％|percent\b)", re.I), _on_percent),
    (re.compile(
        r"百分之(?P<n>百|一百|[一二三四五六七八九]?十[一二三四五六七八九]?|[零〇一二三四五六七八九])"
        r"(?![零〇一二三四五六七八九十百千万亿])",
    ), _on_cn_percent),
    (re.compile(
        rf"(?:\bbetween\s+)?\$\s*(?P<a>{_AMOUNT})\s*(?:-|–|—|to|and)\s*\$?\s*(?P<b>{_AMOUNT})\s*"
        rf"(?P<scale>trillion|billion|million|thousand)\b",
        re.I,
    ), _on_money_span),
    (re.compile(
        r"(?<![0-9零〇一二三四五六七八九两数几好])"
        r"(?P<head>一百|一千|两百|二百|二十|三十|四十|五十|六十|七十|八十|九十|十|[一二三四五六七八九]|百|千)"
        r"(?P<scale>亿|万)"
        r"(?=美元|元|人|台|个|次|部|户|名|家|只|颗|片|条|用户|天|[，。,.、\s]|$)",
    ), _on_cn_scale),
    (re.compile(
        r"(?:(?P<sign>[-−负])\s*)?(?P<a>\d+(?:\.\d+)?|两)\s*亿\s*(?P<b>\d+(?:\.\d+)?)\s*(?P<u>千万|百万|万)",
    ), _on_cn_compound),
    (re.compile(rf"(?P<n>{_AMOUNT})\s*(?:cents?|美分)\b", re.I), _on_cents),
    (re.compile(
        rf"\b(?P<head>half\s+a|half|a|an|{_TENS}-(?:{_ONES})|{_SPELLED_RE})\s+"
        rf"(?:(?P<hundred>hundred)(?:\s+(?P<scale>trillion|billion|million|thousand|percent))?|"
        rf"(?P<scale2>trillion|billion|million|thousand|percent))\b",
        re.I,
    ), _on_spelled_scale),
    (re.compile(
        r"\b(?P<h>\d{1,2})\s*[:：]\s*(?P<mi>\d{2})\s*(?:AM|PM|a\.m\.|p\.m\.)?\b",
        re.I,
    ), _on_clock),
    (re.compile(r"(?P<h>\d{1,2})\s*点\s*(?P<mi>\d{1,2})\s*分"), _on_clock),
    (re.compile(
        r"\b(?:a\s+)?couple\s+of\s+(?:weeks?|months?|days?|hours?)\b",
        re.I,
    ), _on_couple),
    (re.compile(
        rf"\b(?:first|last|next|past)?\s*(?P<n>{_ONES}|ten|eleven|twelve|\d{{1,2}})\s+"
        rf"(?:weeks?|months?|days?|hours?)\b",
        re.I,
    ), _on_duration),
    (re.compile(
        r"(?:头|前|近|过去)(?P<n>[一二三四五六七八九十两]|\d{1,2})\s*个?\s*月",
    ), _on_duration),
    (re.compile(
        r"(?<![0-9零〇一二三四五六七八九两这那每某])(?P<n>[一二三四五六七八九十两])\s*个\s*月",
    ), _on_duration),
    (re.compile(
        r"(?<![0-9零〇一二三四五六七八九两这那每某])(?P<n>[一二三四五六七八九十两])\s*(?:周|小时)",
    ), _on_duration),
    (re.compile(
        rf"\$\s*(?P<a>{_AMOUNT})\b(?:(?!\$).){{0,80}}?\bfor every\s+\$\s*(?P<b>{_AMOUNT})\b",
        re.I,
    ), _on_per),
    (re.compile(
        rf"每(?:获得|有)?[^。\n]{{0,16}}?(?P<b>{_AMOUNT})\s*美元"
        rf"[^。\n]{{0,40}}?(?:约|大约)?\s*(?P<a>{_AMOUNT})\s*美元",
    ), _on_per),
    (re.compile(
        rf"(?:(?P<sign>[-−负])\s*)?"
        rf"(?:(?P<prefix>[$€£¥￥]|(?:USD|EUR|GBP|CNY|RMB|JPY)(?=\s*\d))\s*)?"
        rf"(?P<amount>{_AMOUNT}|两)(?:\s*(?:多|余)?\s*(?P<scale>{_SCALE}))?(?:\s*(?P<currency>{_CURRENCY}))?",
        re.I,
    ), _on_qty),
    (re.compile(
        r"(?<![0-9零〇一二三四五六七八九两数几])(?P<a>[二三四五六七八九两])(?P<b>[二三四五六七八九两])(?=年)",
    ), _on_cn_pair),
    (re.compile(
        r"(?<![0-9零〇一二三四五六七八九两数几])"
        r"(?P<n>二十|三十|四十|五十|六十|七十|八十|九十|十[一二三四五六七八九]?|[二三四五六七八九两])多?"
        r"(?=个|年|次|倍|天|周|月|名|家|项|点|种|大|部|块|条|只|位|款|台|份|岁|样|类|方|端|步|层|轮|代|人)",
    ), _on_cn_measure),
    (re.compile(
        rf"\b(?P<a>{_AMOUNT})\s+out\s+of\s+(?:the\s+)?(?:top\s+)?(?P<b>{_AMOUNT})\b",
        re.I,
    ), _on_out_of),
    (re.compile(
        rf"(?P<b>{_AMOUNT})\s*(?:个|天|人|名|次|家)?[^。\n]{{0,8}}?(?:中|里|内)\s*[，,]?\s*(?:只|仅|共)?\s*有\s*(?P<a>{_AMOUNT})",
    ), _on_out_of),
    (re.compile(r"\b(?P<y>(?:19|20)\d{2})\s*[-–—]\s*(?P<yy>\d{2})\b"), _on_year_span),
    (re.compile(r"\b(?:both|either)\b", re.I), _on_both),
    (re.compile(r"\b(?:twice|twofold|doubled?)\b", re.I), lambda _m: Fact("soft", "2", hard=False)),
    (re.compile(r"\b(?:thrice|threefold|tripled)\b", re.I), lambda _m: Fact("soft", "3", hard=False)),
    (re.compile(r"\b(?:a\s+)?half(?:\s*-\s*|\s+)(?:a\s+)?dozen\b", re.I), lambda _m: Fact("soft", "6", hard=False)),
    (re.compile(r"\ba\s+dozen\b", re.I), lambda _m: Fact("soft", "12", hard=False)),
    (re.compile(r"\b(?:decades?|centur(?:y|ies))\b", re.I), _on_decade),
    (re.compile(r"一百年|(?<![一二三四五六七八九两])百年"), lambda _match: Fact("literal", "100")),
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

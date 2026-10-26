"""问库回答的引用核对。纯规则，不再调模型。

模型被要求每句带 [n]，但没人检查第 n 段里是不是真有这句话。这里逐句对一遍，对不上的列出来：

- 引用了不存在的编号；
- 句子里的数字（两位以上、带小数、百分比、倍数）在所引段落里找不到——数字是证据锚点，张冠李戴最伤；
- 引号里的话在所引段落里找不到；
- 句子点了某位作者的名，所引段落却都不是他写的；
- 句子写了具体日期，和所引段落的发布日期、正文都对不上；
- 和所引段落几乎没有一样的说法（引错了段）。

只查带 [n] 的句子。查出来的是「需要回原文核对」，不一定是错。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_REF = re.compile(r"\[(\d{1,3})\]")
_TRAILING_REFS = re.compile(r"(?:\s*\[\d{1,3}\])+")
_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
_DATE_LIKE = re.compile(
    r"(?<!\d)\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
    r"|(?<!\d)\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
    r"|(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|(?<![\dA-Za-z])(?:FY|Q|H)\d{1,4}"
    r"|(?<!\d)\d{2,4}\s*(?:财年|年度|季度|Q[1-4])"
)
_QUOTED = re.compile(r"「([^」]{4,})」|“([^”]{4,})”|\"([^\"]{4,})\"")
_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_CJK_RUN = re.compile(r"[一-鿿]+")
_FULLWIDTH = str.maketrans("０１２３４５６７８９．％", "0123456789.%")
# 太常见、不说明是不是同一件事的字对。
_COMMON_BIGRAMS = frozenset(
    {
        "认为", "表示", "指出", "提到", "觉得", "判断", "我们", "他们", "这个", "一个", "没有", "可以", "因为", "所以",
        "以及", "已经", "目前", "公司", "作者", "文章", "资料", "观点", "看法", "方面", "问题", "这是", "也是", "就是",
        "还是", "但是", "如果", "这些", "那些", "其中", "可能", "需要", "进行", "通过", "对于", "关于", "之后", "之前",
    }
)
MAX_FLAGS = 20
_OMISSION = re.compile(r"\.{3,}|…+|\[省略\]|（略）")
_QUOTE_TOKEN = re.compile(
    r"[一-鿿]|[^\W\d_一-鿿]+(?:['’][^\W\d_一-鿿]+)*(?:\+\+|#)?"
    r"|[+＋−－-]?[ \t]*(?:\d{1,3}(?:[\u00a0\u202f]\d{3})+(?:[.．]\d+)?"
    r"|\d+(?:[.,，．]\d+)*|[.．]\d+)(?:[ \t]*[%％‰])?"
    r"|(?P<symbol>[^\w\s])"
)
_QUOTE_SYMBOLS = str.maketrans("＋－−￥", "+--¥")
_NUMERIC_TOKEN = re.compile(
    r"(?<![\d.．])([+＋−－-]?[ \t]*(?:\d{1,3}(?:[,，\u00a0\u202f]\d{3})+(?:[.．]\d+)?"
    r"|\d+(?:[.．]\d+)?|[.．]\d+))(?!\d|[.．]\d)"
)
_NUMBER_UNIT = re.compile(
    r"[ \t]*(%|％|‰|个百分点|倍|基点|bps?(?![^\W一-鿿])|basis[ \t]+points?(?![^\W一-鿿])"
    r"|percentage[ \t]+points?(?![^\W一-鿿])|percent(?![^\W一-鿿]))", re.IGNORECASE,
)


def quote_has_omission(quote: str) -> bool:
    return bool(_OMISSION.search(quote or ""))


def _quote_tokens(text: str) -> list[tuple[str, int, int]]:
    tokens = []
    for match in _QUOTE_TOKEN.finditer(text or ""):
        value = match.group()
        if match.lastgroup == "symbol" and not (
            unicodedata.category(value).startswith("S") or value in "%％‰*/&|-－"
        ):
            continue
        value = value.translate(_FULLWIDTH).translate(_QUOTE_SYMBOLS).replace("’", "'").casefold()
        value = re.sub(r"[ \t]+", "", value)
        if re.fullmatch(r"[+-]?\d{1,3}(?:[\u00a0\u202f]\d{3})+(?:\.\d+)?[%‰]?", value):
            value = value.replace("\u00a0", "").replace("\u202f", "")
        if re.fullmatch(r"[+-]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?[%‰]?", value):
            value = value.replace(",", "").replace("，", "")
        value = re.sub(r"^([+-]?)\.(?=\d)", r"\g<1>0.", value)
        tokens.append((value, match.start(), match.end()))
    return tokens


def quote_spans(source: str, quote: str) -> list[tuple[int, int]] | None:
    """Locate every quoted word/number, allowing only explicit omission gaps.

    Punctuation, whitespace and case may differ. English word boundaries,
    numeric signs, decimals and percentage units remain meaningful. This is
    textual location within one source, not verification of a conclusion.
    """
    original = _quote_tokens(source)
    values = [value for value, _, _ in original]
    parts = [[value for value, _, _ in _quote_tokens(part)] for part in _OMISSION.split(quote or "")]
    parts = [part for part in parts if part]
    if not parts:
        return None
    spans = []
    cursor = 0
    for part in parts:
        start = next(
            (index for index in range(cursor, len(values) - len(part) + 1)
             if values[index : index + len(part)] == part),
            None,
        )
        if start is None:
            return None
        end = start + len(part)
        spans.append((original[start][1], original[end - 1][2]))
        cursor = end
    return spans


def _plain(text: str) -> str:
    return "".join(_KEEP.findall((text or "").translate(_FULLWIDTH))).lower()


def _digits(text: str) -> str:
    return re.sub(r"[,，\s]", "", (text or "").translate(_FULLWIDTH))


def _bigrams(text: str) -> set[str]:
    found: set[str] = set()
    for run in _CJK_RUN.findall(text or ""):
        for index in range(len(run) - 1):
            pair = run[index : index + 2]
            if pair not in _COMMON_BIGRAMS:
                found.add(pair)
    return found


def split_claims(answer: str) -> list[tuple[str, list[int]]]:
    """回答切成带出处的句子：[(句子, [n...])]。[n] 写在句号后面的也算这一句的。"""
    claims: list[tuple[str, list[int]]] = []
    answer = answer or ""
    closers = []
    pairs = {"「": "」", "“": "”", '"': '"'}
    quote_marks = set(pairs) | set(pairs.values())
    start = index = 0
    while index < len(answer):
        char = answer[index]
        escaped = 0
        if char in quote_marks:
            previous = index - 1
            while previous >= 0 and answer[previous] == "\\":
                escaped += 1
                previous -= 1
        if char in quote_marks and escaped % 2 == 0:
            if closers and char == closers[-1]:
                closers.pop()
            elif char in pairs:
                closers.append(pairs[char])
        index += 1
        if index < len(answer) and (closers or char not in "。！？!?；;\n"):
            continue
        # A reference after a final stop belongs to the preceding sentence.
        trailing = _TRAILING_REFS.match(answer, index)
        if trailing:
            index = trailing.end()
        text = answer[start:index].strip()
        refs = [int(item) for item in _REF.findall(text)]
        if refs:
            claims.append((text, list(dict.fromkeys(refs))))
        start = index
    return claims


def _numeric_anchors(text: str, *, significant_only: bool = False) -> list[tuple[str, str]]:
    """Keep numeric token boundaries, signs and units from actual passage text.

    Only conventional grouping and equivalent unit spellings are normalized;
    unrelated numbers separated by whitespace are never concatenated.
    """
    found: list[tuple[str, str]] = []
    for match in _NUMERIC_TOKEN.finditer(text):
        number = match.group(1).translate(_FULLWIDTH).translate(_QUOTE_SYMBOLS)
        number = re.sub(r"[,，\u00a0\u202f \t]", "", number)
        number = re.sub(r"^([+-]?)\.(?=\d)", r"\g<1>0.", number)
        sign = "-" if number.startswith("-") else ""
        whole, _, fraction = number.lstrip("+-").partition(".")
        magnitude = (whole.lstrip("0") or "0") + ("." + fraction.rstrip("0") if fraction.rstrip("0") else "")
        number = (sign if magnitude != "0" else "") + magnitude
        unit = ""
        if suffix := _NUMBER_UNIT.match(text, match.end()):
            unit = suffix.group(1).translate(_FULLWIDTH).casefold()
            if unit in {"bp", "bps", "基点"} or unit.startswith("basis"):
                unit = "bp"
            elif unit.startswith("percentage"):
                unit = "个百分点"
            elif unit == "percent":
                unit = "%"
        if significant_only and not unit and "." not in match.group(1).translate(_FULLWIDTH):
            if len(magnitude) < 2 or (len(magnitude) == 4 and 1990 <= int(magnitude) <= 2039):
                continue
        found.append((number, unit))
    return found


def _numbers(text: str) -> list[str]:
    body = _DATE_LIKE.sub(" ", _REF.sub(" ", text))
    return list(dict.fromkeys(number + unit for number, unit in _numeric_anchors(body, significant_only=True)))


def _iso(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def check_claim(text: str, refs: list[int], citations: list[dict], authors: Iterable[str]) -> list[str]:
    problems: list[str] = []
    by_number = {int(item["n"]): item for item in citations if "n" in item}
    missing = [n for n in refs if n not in by_number]
    if missing:
        problems.append("引用了不存在的出处 " + "".join(f"[{n}]" for n in missing))
    cited = [by_number[n] for n in refs if n in by_number]
    if not cited:
        return problems
    raw = "\n".join(
        f"{item.get('title') or ''}\n{item.get('heading') or ''}\n{item.get('before') or ''}"
        f"\n{item.get('text') or ''}\n{item.get('after') or ''}"
        for item in cited
    )
    digits = _digits(raw)
    plain = _plain(raw)
    quote_sources = [
        "\n".join(str(item.get(key) or "") for key in ("before", "text", "after"))
        for item in cited
    ]
    numeric_evidence = {number + unit for source in quote_sources for number, unit in _numeric_anchors(source)}
    for number in _numbers(text):
        if number not in numeric_evidence:
            problems.append(f"数字 {number} 不在所引段落里")
    for match in _QUOTED.finditer(text):
        quoted = next(group for group in match.groups() if group)
        if _plain(quoted) and not any(quote_spans(source, quoted) for source in quote_sources):
            problems.append(f"引号里的「{quoted[:20]}」在所引段落里找不到")
    cited_authors = {item.get("author") or "" for item in cited}
    named = [name for name in authors if name and name in text]
    for name in named:
        if name not in cited_authors and not any(name in (item.get("text") or "") for item in cited):
            problems.append(f"所引段落不是{name}写的")
    cited_dates = {item.get("date") or "" for item in cited}
    for match in _FULL_DATE.finditer(text):
        wanted = _iso(*match.groups())
        if wanted in cited_dates:
            continue
        compact = wanted.replace("-", "")
        if compact in digits or _plain(match.group(0)) in plain:
            continue
        problems.append(f"日期 {wanted} 和所引段落对不上")
    stripped = _FULL_DATE.sub(" ", _REF.sub(" ", text))
    for name in named:
        stripped = stripped.replace(name, " ")
    claim_pairs = _bigrams(stripped)
    if len(claim_pairs) >= 6 and not (claim_pairs & _bigrams(raw)):
        problems.append("和所引段落几乎没有一样的说法")
    return problems


def check_answer(answer: str, citations: list[dict], authors: Iterable[str]) -> list[dict]:
    """Return rule warnings. An empty list does not establish semantic support."""
    names = sorted({name for name in authors if name}, key=len, reverse=True)
    flags: list[dict] = []
    for text, refs in split_claims(answer):
        problems = check_claim(text, refs, citations or [], names)
        if problems:
            sentence = re.sub(r"^\s*(?:[-*+]|\d+[.、)])\s*", "", _REF.sub("", text)).strip(" #*")
            flags.append({"sentence": sentence, "refs": refs, "problems": problems})
            if len(flags) >= MAX_FLAGS:
                break
    return flags

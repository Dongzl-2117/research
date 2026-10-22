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
from collections.abc import Iterable

_CLAIM = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;]+)?(?:\s*\[\d{1,3}\])*")
_REF = re.compile(r"\[(\d{1,3})\]")
_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
_DATE_LIKE = re.compile(
    r"(?<!\d)\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
    r"|(?<!\d)\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
    r"|(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|(?<![\dA-Za-z])(?:FY|Q|H)\d{1,4}"
    r"|(?<!\d)\d{2,4}\s*(?:财年|年度|季度|Q[1-4])"
)
_NUMBER = re.compile(r"(?<![\d.])(\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*(%|％|‰|倍|个百分点|bp)?")
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
    for match in _CLAIM.finditer(answer or ""):
        text = match.group(0).strip()
        refs = [int(item) for item in _REF.findall(text)]
        if refs:
            claims.append((text, list(dict.fromkeys(refs))))
    return claims


def _numbers(text: str) -> list[str]:
    body = _REF.sub(" ", text)
    body = _DATE_LIKE.sub(" ", body)
    found: list[str] = []
    for match in _NUMBER.finditer(body.translate(_FULLWIDTH)):
        raw, unit = match.group(1), match.group(2) or ""
        number = _digits(raw)
        if not unit and "." not in number and len(number) < 2:
            continue
        if not unit and len(number) == 4 and 1990 <= int(number) <= 2039:
            continue
        found.append(number + unit)
    return list(dict.fromkeys(found))


def _number_in(number: str, haystack: str) -> bool:
    core = re.match(r"[\d.]+", number).group(0)
    return re.search(r"(?<![\d.])" + re.escape(core) + r"(?!\d)", haystack) is not None


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
    for number in _numbers(text):
        if not _number_in(number, digits):
            problems.append(f"数字 {number} 不在所引段落里")
    for match in _QUOTED.finditer(text):
        quoted = next(group for group in match.groups() if group)
        if _plain(quoted) and _plain(quoted) not in plain:
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
    """返回没核对上的句子：[{"sentence", "refs", "problems"}]。都对得上时是空列表。"""
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

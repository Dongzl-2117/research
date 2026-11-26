"""问库回答的引用核对。纯规则，不再调模型。

模型被要求每句带 [n]，但没人检查第 n 段里是不是真有这句话。这里逐句对一遍，对不上的列出来：

- 引用了不存在的编号；
- 句子里的数字（两位以上、带小数、百分比、倍数）在所引段落里找不到——数字是证据锚点，张冠李戴最伤；
- 引号里的话在所引段落里找不到；
- 句子点了某位作者的名，所引段落却都不是他写的；
- 句子写了具体日期，和所引段落的发布日期、正文都对不上；
- 和所引段落几乎没有一样的说法（引错了段）。

没有出处的陈述句也会提示。查出来的是「需要回原文核对」，不一定是错。

所引段落是没译的英文原文时，中文句子和它字对字比不了：不查「几乎没有一样的说法」，中文引语也不按原文找；
数字照查，英文拼出来的数（forty-seven、two hundred）也算；换了单位的按数值比（4700 万 = 47 million，
32 亿 = $3.2 billion）。引语和原文只差「的」「了」这类虚字的不算对不上。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from researchlib.financial_evidence import Number, numbers

_CLAIM = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;]+)?(?:\s*\[\d{1,3}\])*")
_INLINE_QUOTE = re.compile(r'「[^「」\n]*」|“[^“”\n]*”|"[^"\n]*"')
_QUOTE_STOPS = "。！？!?；;"
_QUOTED_PUNCTUATION = str.maketrans(dict.fromkeys(_QUOTE_STOPS, " "))
_AFTER_QUOTE_REF = re.compile(r"[ \t]*\[\d{1,3}\]")
_REF = re.compile(r"\[(\d{1,3})\]")
_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
# 「2026-03-18/09-01」「2025-07-30、10-27」：同一年里的几个日期，后面的省了年份。补全了再核对，
# 否则省掉年份的月和日被当成数字（「数字 09 不在所引段落里」）。后面跟着量级、单位的是数的范围，不算日期。
_DATE_LIST = re.compile(
    r"(?<!\d)(?P<year>\d{4})(?P<sep>[-.])\d{1,2}(?P=sep)\d{1,2}(?!\d)"
    r"(?P<rest>(?:\s*[/、，,和及与]\s*(?:0?[1-9]|1[0-2])(?P=sep)(?:0?[1-9]|[12]\d|3[01])(?!\d|\s*[%％‰万亿倍元个]))+)"
)
_SHORT_DAY = re.compile(r"(?<![\d.-])(\d{1,2})([-.])(\d{1,2})(?![\d.])")
_DATE_LIKE = re.compile(
    r"(?<!\d)\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
    r"|(?<!\d)\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
    r"|(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日"
    # 只写月份（「12月」）、两位年份（「24年」）：和所引段落的发布日期一起看，不当数字核。「24年代」不算。
    r"|(?<![\d.])(?:1[0-2]|0?[1-9])\s*月(?!\d)"
    r"|(?<![\d.])\d{2}\s*年(?![代\d])"
    r"|(?<![\dA-Za-z])(?:FY|Q|H)\d{1,4}"
    r"|(?<!\d)\d{2,4}\s*(?:财年|年度|季度|Q[1-4])"
)
# 回答分点写的序号（「10. 」「3、」「(2) 」）不是要核的数。
_LIST_MARK = re.compile(r"(?m)^\s*(?:[-*+]\s*)?(?:\d{1,3}[.、)）](?!\d)|[(（]\d{1,3}[)）])\s*")
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
_CJK_CHAR = re.compile(r"[一-鿿]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")
# 汉字占字母和汉字合计不到这个比例，算外文段落。
FOREIGN_CJK_SHARE = 0.3
# 引语里这些虚字多一个少一个不改意思，比对时去掉。
_PARTICLES = str.maketrans("", "", "的了地得之着")
_EN_SMALL = {
    word: value
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"
        " sixteen seventeen eighteen nineteen".split()
    )
}
_EN_TENS = {
    word: 10 * value
    for value, word in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split())
    if word != "_"
}
_EN_SCALES = {"hundred": 100, "thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12}
_EN_WORD = "|".join(sorted([*_EN_SMALL, *_EN_TENS, *_EN_SCALES], key=len, reverse=True))
_EN_NUMBER = re.compile(rf"\b(?:{_EN_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_EN_WORD}))*\b", re.IGNORECASE)


def _plain(text: str) -> str:
    return "".join(_KEEP.findall((text or "").translate(_FULLWIDTH))).lower()


def _digits(text: str) -> str:
    """数字之间的千分位逗号去掉；空白收成一个空格，不删：「收入 100\n2024 年」删了空白就粘成 1002024。"""
    text = re.sub(r"(?<=\d)[,，](?=\d)", "", (text or "").translate(_FULLWIDTH))
    return re.sub(r"\s+", " ", text)


def _loose(text: str) -> str:
    return _plain(text).translate(_PARTICLES)


def _foreign(text: str) -> bool:
    """主要是外文（多半是没译的英文原文）。"""
    cjk = len(_CJK_CHAR.findall(text or ""))
    latin = len(_LATIN_CHAR.findall(text or ""))
    return latin > 0 and cjk < FOREIGN_CJK_SHARE * (cjk + latin)


def _english_numbers(text: str) -> list[str]:
    """英文里拼出来的数换成阿拉伯数字：forty-seven → 47，two hundred → 200，a/one million → 1000000。"""
    found: list[str] = []
    for match in _EN_NUMBER.finditer(text or ""):
        total = current = 0
        for word in re.split(r"[\s-]+", match.group(0).lower()):
            if word in _EN_SMALL:
                current += _EN_SMALL[word]
            elif word in _EN_TENS:
                current += _EN_TENS[word]
            elif word == "hundred":
                current = (current or 1) * 100
            elif word in _EN_SCALES:
                total += (current or 1) * _EN_SCALES[word]
                current = 0
        found.append(str(total + current))
    return found


def _bigrams(text: str) -> set[str]:
    found: set[str] = set()
    for run in _CJK_RUN.findall(text or ""):
        for index in range(len(run) - 1):
            pair = run[index : index + 2]
            if pair not in _COMMON_BIGRAMS:
                found.add(pair)
    return found


def split_claims(answer: str, *, include_uncited: bool = False) -> list[tuple[str, list[int]]]:
    """回答切成带出处的句子：[(句子, [n...])]。[n] 写在句号后面的也算这一句的。"""
    def mask_quote(match: re.Match) -> str:
        quote = match.group()
        masked = quote.translate(_QUOTED_PUNCTUATION)
        # 引语自己结束一句并紧跟 [n] 时，把分界放在闭引号后；「……。」说明……[n] 则是同一句。
        if quote[-2] in _QUOTE_STOPS and _AFTER_QUOTE_REF.match(match.string, match.end()):
            return masked[:-1] + "。"
        return masked

    # 只在等长副本上找分界，核对和分批摘录始终取回答原字。未闭合的引号不跨行吞掉后文。
    masked = _INLINE_QUOTE.sub(mask_quote, answer or "")
    claims: list[tuple[str, list[int]]] = []
    for match in _CLAIM.finditer(masked):
        text = answer[match.start():match.end()].strip()
        refs = [int(item) for item in _REF.findall(text)]
        statement = text.rstrip("* ").strip()
        uncited = (
            include_uncited and len(_plain(statement)) >= 4
            and not statement.startswith(("#", "|", "`"))
            and not statement.endswith(("？", "?", "：", ":"))
            and statement.strip("。！! ") not in {"资料不足", "库里没有相关内容", "没有相关内容", "资料里没看到"}
        )
        if refs or uncited:
            claims.append((text, list(dict.fromkeys(refs))))
    return claims


def _claim_numbers(text: str):
    body = _DATE_LIKE.sub(" ", _LIST_MARK.sub(" ", _REF.sub(" ", text)))
    for number in numbers(body):
        if number.label.isdigit():
            if len(number.label) < 2 or 1990 <= number.value <= 2039:
                continue
        yield number


def _evidence_numbers(text: str) -> set[tuple]:
    # 数位后面的 million 等留给量级解析；forty-seven million 等整体换算。
    def expand(match):
        token = match.group()
        prefix = text[max(0, match.start() - 5):match.start()]
        if token.lower() in _EN_SCALES and not re.search(r"\ba\s*$", prefix, re.I):
            return token
        return _english_numbers(token)[0]

    return {(n.value, n.unit) for n in numbers(_EN_NUMBER.sub(expand, text))}


def _supported(number: Number, evidence: set[tuple]) -> bool:
    if (number.value, number.unit) in evidence:
        return True
    # 仅允许常用的人民币/元两种写法，不把美元或港元当人民币。
    if number.unit == "元":
        return (number.value, "CNY") in evidence
    if number.unit == "CNY":
        return (number.value, "元") in evidence
    return False


def _iso(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _expand_dates(text: str) -> str:
    """「2026-03-18/09-01」补成「2026-03-18/2026-09-01」，后面省了年份的日期按前一个的年份补。"""

    def full(match: re.Match) -> str:
        year = match.group("year")
        rest = _SHORT_DAY.sub(lambda day: f"{year}{day[2]}{day[1]}{day[2]}{day[3]}", match["rest"])
        return match.group(0)[: match.start("rest") - match.start()] + rest

    return _DATE_LIST.sub(full, text)


def check_claim(text: str, refs: list[int], citations: list[dict], authors: Iterable[str]) -> list[str]:
    text = _expand_dates(text)
    if not refs:
        return ["这句没有标注出处"]
    problems: list[str] = []
    by_number = {int(item["n"]): item for item in citations if "n" in item}
    missing = [n for n in refs if n not in by_number]
    if missing:
        problems.append("引用了不存在的出处 " + "".join(f"[{n}]" for n in missing))
    unread = [n for n in refs if by_number.get(n, {}).get("read_failed")]
    if unread:
        problems.append("所引出处所在批次未读到 " + "".join(f"[{n}]" for n in unread))
    cited = [by_number[n] for n in refs if n in by_number and n not in unread]
    if not cited:
        return problems
    raw = "\n".join(
        f"{item.get('date') or ''}\n{item.get('title') or ''}\n{item.get('heading') or ''}\n{item.get('before') or ''}"
        f"\n{item.get('text') or ''}\n{item.get('after') or ''}"
        for item in cited
    )
    # 标题常是译好的中文，判断段落是不是外文只看章节和正文。
    foreign = _foreign(
        "\n".join(item.get(key) or "" for item in cited for key in ("heading", "before", "text", "after"))
    )
    digits = _digits(raw)
    evidence = _evidence_numbers(raw)
    plain = _plain(raw)
    loose = _loose(raw)
    for number in _claim_numbers(text):
        if not _supported(number, evidence):
            if not number.unit and (number.value, "%") in evidence:
                problems.append(f"回答写作 {number.label}，出处中相同数值带百分号，请核对单位")
            else:
                problems.append(f"数字 {number.label} 不在所引段落里")
    for match in _QUOTED.finditer(text):
        quoted = next(group for group in match.groups() if group)
        if not _plain(quoted) or _plain(quoted) in plain or _loose(quoted) in loose:
            continue
        if foreign and not _foreign(quoted):
            # 英文原话译成了中文再加引号：字对字比不了，不算对不上。
            continue
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
    if not foreign and len(claim_pairs) >= 6 and not (claim_pairs & _bigrams(raw)):
        problems.append("和所引段落几乎没有一样的说法")
    return problems


def check_answer(answer: str, citations: list[dict], authors: Iterable[str]) -> list[dict]:
    """返回没核对上的句子：[{"sentence", "refs", "problems"}]。都对得上时是空列表。"""
    names = sorted({name for name in authors if name}, key=len, reverse=True)
    flags: list[dict] = []
    for text, refs in split_claims(answer, include_uncited=True):
        problems = check_claim(text, refs, citations or [], names)
        if problems:
            sentence = re.sub(r"^\s*(?:[-*+]|\d+[.、)])\s*", "", _REF.sub("", text)).strip(" #*")
            flags.append({"sentence": sentence, "refs": refs, "problems": problems})
            if len(flags) >= MAX_FLAGS:
                break
    return flags

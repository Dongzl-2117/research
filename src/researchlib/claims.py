"""观点和论点共用的文本判断：引用规范化、论证是否落在这家公司、和论点同向还是反向。

不按作者对公司的总态度（看好 / 看淡）分边。总态度是看好的人，也可以反对「现在就该提价」。
"""

from __future__ import annotations

import re
import unicodedata

_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_SPACE = re.compile(r"[\s\u200b\u200c\u200d\ufeff\u00ad]+")
# 论点里被肯定的说法，和被否定的说法。一对同时出现时，否定优先。
_AFFIRM = re.compile(
    r"提价|涨价|吃掉|被吃|承压|被压|受损|好卖|动销好|动销强|动销很?好|还能抬|没到顶|还有空间"
)
_DENY = re.compile(
    r"不宜提价|不应提价|不能提价|暂不提价|先不提价|提不了价|现阶段不宜|"
    r"没被吃掉|未被吃掉|没有被吃掉|还能守住|守得住|影响有限|影响不大|转嫁出去|"
    r"不好卖|并不好卖|动销放缓|动销偏弱|见顶|抬不上去|加载率抬不"
)
_HEDGE = re.compile(r"先观察|先不说|再看看|不确定|不好说")
_FAIL = re.compile(r"失败|放缓|并不|不是|不稳|不宜|不能|没有|见顶|抬不上去")
_LIST = re.compile(r"例如|比如|譬如|顺带|顺便|提一句|其中包括|等公司|等标的|四小龙|之一")
_PRE_NEG = (
    "不应该",
    "不宜",
    "不应",
    "不能",
    "不可",
    "没有",
    "并未",
    "并不",
    "不是",
    "难以",
    "暂不",
    "还不",
    "不会",
    "不该",
    "不必",
    "没法",
    "没能",
    "没被",
    "未被",
    "不",
    "没",
    "未",
)


def plain_cite(text: str) -> str:
    """比对引用：全角转半角，去掉空白、换行和零宽字符，只留汉字、字母和数字。"""
    folded = unicodedata.normalize("NFKC", text or "")
    folded = _SPACE.sub("", folded)
    return "".join(_KEEP.findall(folded)).lower()


def quote_ok(quote: str) -> bool:
    """空引用、纯换行、短到对不上原文的，拒收。"""
    return len(plain_cite(quote)) >= 6


# 原话只允许排版差异。用于粗匹配的 plain_cite 会抹掉负号、小数点、币种和单位。
_WIDTH = str.maketrans({**{c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}, ord("−"): "-"})
_NUM_PREFIX = "0123456789.,+-−$€£¥￥<>≤≥"
_NUM_SUFFIX = "0123456789%‰万亿千百倍元"


def _quote_key(text: str) -> tuple[str, list[int]]:
    chars, offsets = [], []
    for index, char in enumerate(text):
        if char.isspace() or char in "\u200b\ufeff":
            # 1 23 和 12 3、两个英文单词的边界仍然有意义。
            if chars and chars[-1].isascii() and chars[-1].isalnum():
                following = text[index + 1 : index + 2].translate(_WIDTH)
                if following.isascii() and following.isalnum():
                    chars.append(" ")
                    offsets.append(index)
            continue
        chars.append(char.translate(_WIDTH))
        offsets.append(index)
    return "".join(chars), offsets


def source_quote(source: str, quote: str) -> str:
    """在原文定位，返回源字符区间。拒绝截掉数值的符号、精度或单位。"""
    if not source or not quote_ok(quote):
        return ""
    quote = quote.strip()
    # 按原样匹配且两头都不是数时无需为整段建立字符偏移表。
    if not quote[0].isdigit() and not quote[-1].isdigit() and quote in source:
        return quote
    key, _ = _quote_key(quote)
    text, offsets = _quote_key(source)
    start = text.find(key)
    while start >= 0:
        end = start + len(key)
        bad_start = key[0].isdigit() and start > 0 and text[start - 1] in _NUM_PREFIX
        bad_end = key[-1].isdigit() and end < len(text) and (
            text[end] in _NUM_SUFFIX or (text[end].isascii() and text[end].isalpha())
            or (text[end] in ".," and text[end + 1 : end + 2].isdigit())
        )
        if not bad_start and not bad_end:
            return source[offsets[start] : offsets[end - 1] + 1]
        start = text.find(key, start + 1)
    return ""


def looks_like_list(text: str, company: str = "") -> bool:
    """列举、顺带、举例。这种句子不是对这家公司的论证。"""
    body = text or ""
    if not body:
        return False
    if body.count("、") >= 2 and _LIST.search(body) and (not company or company in body):
        return True
    if _LIST.search(body) and len(body) <= 80 and (not company or company in body):
        return True
    return False


def _ends_with_neg(window: str) -> bool:
    tail = window[-8:]
    for prefix in _PRE_NEG:
        if tail.endswith(prefix):
            return True
    return False


def _negated_bigrams(text: str) -> set[str]:
    raw = text or ""
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(raw):
        if "\u4e00" <= char <= "\u9fff":
            chars.append(char)
            positions.append(index)
    found: set[str] = set()
    for index in range(len(chars) - 1):
        gram = chars[index] + chars[index + 1]
        window = raw[max(0, positions[index] - 8) : positions[index]]
        if _ends_with_neg(window):
            found.add(gram)
    return found


def _bigrams(text: str) -> set[str]:
    chars = [char for char in text or "" if "\u4e00" <= char <= "\u9fff"]
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


def is_hedge(text: str) -> bool:
    """先观察、先不说：没有明确反对时，不要把它算进支持或反对。"""
    return _HEDGE.search(text or "") is not None


# 仅对指标、比较基准和期间可对齐的单一断言判方向，复合判断交给来源的分边。
_TREND = re.compile(
    r"(?P<metric>毛利率|净利率|利润率|营业收入|营收|净利润|销售额|销量|出货量|订单量|渠道库存|库存|"
    r"毛利|费用率|现金流|资本开支|广告加载率|客单价)"
    r"(?P<basis>同比|环比)?(?:将会|预计|有望|已经|继续|持续|出现|逐步|保持|将|会|已|在)*"
    r"(?P<trend>增长|增加|升高|上升|上涨|提升|攀升|改善|下降|下滑|降低|减少|恶化|持平|不变|稳定)"
)
_UP = {"增长", "增加", "升高", "上升", "上涨", "提升", "攀升", "改善"}
_FLAT = {"持平", "不变", "稳定"}
_SEGMENT = re.compile(r"国内|海外|国际|中国区|北美|欧洲|线上|线下|零售|批发|直营|加盟")
_PERIOD = re.compile(r"去年|今年|明年|上年|本年|下年|上季度?|本季度?|下季度?|[12]\d{3}年?|Q[1-4]|[一二三四]季度")


def _trend_facts(text: str) -> set[tuple]:
    facts = set()
    for clause in re.split(r"[，。；;！!?？]", text):
        periods = tuple(sorted(set(_PERIOD.findall(clause))))
        for match in _TREND.finditer(clause):
            trend = match["trend"]
            direction = 1 if trend in _UP else 0 if trend in _FLAT else -1
            segments = tuple(sorted(set(_SEGMENT.findall(clause))))
            facts.add((match["metric"], match["basis"] or "", periods, segments, direction))
    return facts


def claim_side(thesis_text: str, view_text: str) -> str:
    """这条观点相对论点的判断：support、oppose 或 unclear。

    不看作者对整家公司是看好还是看淡。说不清的不猜，调用方不要退回总态度。
    """
    thesis = thesis_text or ""
    view = view_text or ""
    if not view.strip():
        return "unclear"
    left, right = _trend_facts(thesis), _trend_facts(view)
    if left and right:
        if _HEDGE.search(view) or _HEDGE.search(thesis):
            return "unclear"
        if len(left) == len(right) == 1:
            a, b = next(iter(left)), next(iter(right))
            if a[:-1] == b[:-1]:
                return "support" if a[-1] == b[-1] else "oppose"
        return "unclear"
    thesis_yes = bool(_AFFIRM.search(thesis)) and not bool(_DENY.search(thesis))
    thesis_no = bool(_DENY.search(thesis)) and not bool(_AFFIRM.search(thesis))
    view_yes = bool(_AFFIRM.search(view))
    view_no = bool(_DENY.search(view))
    if thesis_yes and view_no:
        return "oppose"
    if thesis_no and view_yes and not view_no:
        return "oppose"
    if thesis_yes and view_yes and not view_no and not _FAIL.search(view):
        return "support"
    if thesis_no and view_no and not view_yes:
        return "support"
    if _FAIL.search(view) and not _FAIL.search(thesis) and (view_no or thesis_yes or thesis_no):
        return "oppose"
    if _HEDGE.search(view) and not view_no and not view_yes:
        return "unclear"
    thesis_neg = _negated_bigrams(thesis)
    view_neg = _negated_bigrams(view)
    shared_pos = (_bigrams(thesis) - thesis_neg) & (_bigrams(view) - view_neg)
    if len(view_neg & (_bigrams(thesis) - thesis_neg)) >= 1:
        return "oppose"
    if len(shared_pos & thesis_neg) >= 1 and not (view_neg & thesis_neg):
        return "oppose"
    if _HEDGE.search(view):
        return "unclear"
    clauses = re.split(r"[，。；;！？!?\s]+", thesis)
    matched_clause = any(len(phrase) >= 5 and phrase in view for phrase in clauses)
    if matched_clause or view.strip() in thesis or len(view_neg & thesis_neg) >= 1:
        return "support"
    return "unclear"


# 转贴比的两段字各要多少字才算数：判断 12 字、原句 8 字。再短的常常只是碰巧一样。
REPOST_LEAST = (12, 8)


def repost_key(view: dict) -> tuple[str, str]:
    """一条观点拿去认转贴的两段字：规范化之后的判断和原句。一条只算一次，和很多条比时不用重算。"""
    judgment = _quote_key(str(view.get("judgment") or ""))[0]
    quote = _quote_key(str(view.get("quote") or ""))[0]
    return judgment, quote


def same_key(left: tuple[str, str], right: tuple[str, str]) -> bool:
    """两条观点的 repost_key 是不是同一篇转贴：判断或原句，两边都够长、一边包含另一边。"""
    for mine, theirs, least in zip(left, right, REPOST_LEAST, strict=True):
        if len(mine) >= least and len(theirs) >= least and (mine in theirs or theirs in mine):
            return True
    return False


def same_repost(left: dict, right: dict) -> bool:
    """公众号和星球转贴：判断或原句规范化之后互相包含，就当作同一篇，不要两边都计。"""
    return (left.get("author") or "") == (right.get("author") or "") and same_key(repost_key(left), repost_key(right))

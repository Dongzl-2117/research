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


def claim_side(thesis_text: str, view_text: str) -> str:
    """这条观点相对论点的判断：support、oppose 或 unclear。

    不看作者对整家公司是看好还是看淡。说不清的不猜，调用方不要退回总态度。
    """
    thesis = thesis_text or ""
    view = view_text or ""
    if not view.strip():
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
    if len(shared_pos) >= 2 or len(view_neg & thesis_neg) >= 1:
        return "support"
    return "unclear"


def same_repost(left: dict, right: dict) -> bool:
    """公众号和星球转贴：判断或原句规范化之后互相包含，就当作同一篇，不要两边都计。"""
    left_judgment = plain_cite(str(left.get("judgment") or ""))
    right_judgment = plain_cite(str(right.get("judgment") or ""))
    if len(left_judgment) >= 12 and len(right_judgment) >= 12 and (
        left_judgment in right_judgment or right_judgment in left_judgment
    ):
        return True
    left_quote = plain_cite(str(left.get("quote") or ""))
    right_quote = plain_cite(str(right.get("quote") or ""))
    return bool(
        len(left_quote) >= 8
        and len(right_quote) >= 8
        and (left_quote in right_quote or right_quote in left_quote)
    )

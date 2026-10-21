"""方伟知识星球的展示标题。

星球导出经常没有标题，或者把第一句截成「……」。卡片、文章页和重新导入时用这里的规则：

- 完整的第一句（问句也算）优先，去掉句号，一般不超过 40 字。
- 第一句太碎（半截、寒暄、只有「见图」）时，用摘要的第一句压成标题。
- 还不够，就用图片里的字，再不行用日期加几个字。
- 标题不能空着，也不能以「…」或「...」结尾，不用「无标题」这类占位。

读者帖的「读者帖」标在作者行，不写进标题。
"""

from __future__ import annotations

import re

from researchlib.textutil import argumentative_excerpt

FANGWEI_ZSXQ = "fangwei-zsxq"
TITLE_LIMIT = 40

_ELLIPSIS_END = re.compile(r"(?:\s*(?:…+|⋯+|\.{3,}|。{2,}|．{3,}))+\s*$")
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKUP = re.compile(r"[*_`]+")
_LEAD = re.compile(
    r"^(?:"
    r"(?:方伟|作者|星主|笔者认为)(?:认为|觉得|指出|提到|说|表示|写道)[，,：:\s]*"
    r"|这篇(?:文章|短帖)?(?:认为|在讲|讨论|说的是|写的是)[，,：:\s]*"
    r"|(?:读者问|作者答|作者补充)\s*[：:]\s*"
    r")"
)
_SPAM = re.compile(r"扫码|二维码|长按识别|加微信|优惠券")
_FILLER = re.compile(
    r"^(?:大家好|各位好|早上好|晚上好|分享一下|转一下|如题|同上|呵呵+|哈哈+|嗯+|这个|就是|如下|见图|看图|上图|下图)"
    r"[，,。！？!?\s]*$"
)
_DANGLING = re.compile(r"[，,；;]\s*([^，,；;]{0,3})$")
_CJK = re.compile(r"[一-鿿]")
_MEANINGFUL = re.compile(r"[0-9A-Za-z一-鿿]")
_PLACEHOLDER = frozenset({"无标题", "未命名", "无题", "暂无标题", "图片", "见图", "短帖"})
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}(?:\s+短帖)?")
_BREAKS = "，,；;、：:"


def repairs_source(source_id: str) -> bool:
    return source_id == FANGWEI_ZSXQ


def title_needs_repair(title: str) -> bool:
    """空标题、占位、纯话题号，或以省略号结尾。"""
    text = (title or "").strip()
    if not text or _placeholder(text) or _ends_with_ellipsis(text):
        return True
    if re.fullmatch(r"\d{5,}", text):
        return True
    if _DATE_ONLY.fullmatch(text):
        return True
    return False


def zsxq_display_title(
    title: str,
    *,
    body: str = "",
    summary: str = "",
    ocr: str = "",
    when: str = "",
) -> str:
    """给卡片和文章页用的标题。原标题能用就原样返回。"""
    stored = (title or "").strip()
    if stored and not title_needs_repair(stored):
        return stored
    body_solid, body_salvage = _from_text(body)
    if body_solid:
        return body_solid
    ocr_solid, ocr_salvage = _from_text(ocr)
    if ocr_solid:
        return ocr_solid
    summary_solid, summary_salvage = _from_text(summary)
    if summary_solid:
        return summary_solid
    for salvage in (body_salvage, ocr_salvage, summary_salvage, _salvage_stored(stored)):
        if salvage:
            return salvage
    snippet = _short_snippet(body) or _short_snippet(ocr) or _short_snippet(summary)
    date = _date_prefix(when)
    if date and snippet:
        fitted = _fit(f"{date} {snippet}")
        if fitted:
            return fitted
    if snippet:
        return snippet
    if date:
        return f"{date} 短帖"
    return "短帖"


def stored_zsxq_title(title: str, body: str) -> str | None:
    """导入时能从正文写成一句完整标题才覆盖库里的标题。

    日期加「短帖」这种兜底不写入：下次打开页面还可以用摘要或图片里的字补。
    原标题本来就能用时返回 None。
    """
    if not title_needs_repair(title):
        return None
    solid, _salvage = _from_text(body)
    if not solid or title_needs_repair(solid) or _ends_with_ellipsis(solid):
        return None
    return solid


def _from_text(text: str) -> tuple[str, str]:
    solid = ""
    salvage = ""
    for sentence, truncated in _pull_sentences(text):
        prepared = _prepare(sentence)
        if _ends_with_ellipsis(prepared):
            prepared = _strip_ellipsis(prepared)
        if not prepared:
            continue
        if not truncated and not _dangling(prepared) and _ok(prepared):
            fitted = _fit(prepared)
            if fitted and not _ends_with_ellipsis(fitted):
                return fitted, salvage
        clipped = _fit(_clip_dangling(prepared))
        if clipped and _ok(clipped) and not _ends_with_ellipsis(clipped) and not salvage:
            salvage = clipped
    return solid, salvage


def _pull_sentences(text: str) -> list[tuple[str, bool]]:
    remaining = _plain(text)
    if not remaining.strip():
        return []
    found: list[tuple[str, bool]] = []
    for _ in range(3):
        raw = argumentative_excerpt(remaining, limit=800)
        if not raw:
            break
        truncated = _ends_with_ellipsis(raw.strip())
        sentence = _strip_ellipsis(raw).strip()
        if not sentence:
            break
        if _spammy(sentence):
            if raw in remaining:
                remaining = remaining.replace(raw, " ", 1)
                continue
            break
        found.append((sentence, truncated))
        break
    return found


def _plain(text: str) -> str:
    cleaned = _IMAGE.sub(" ", text or "")
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _MARKUP.sub("", cleaned)
    return cleaned


def _prepare(sentence: str) -> str:
    current = sentence.strip()
    for _ in range(2):
        match = _LEAD.match(current)
        if not match:
            break
        rest = current[match.end() :].strip()
        if len(_CJK.findall(rest)) < 4 and len(_MEANINGFUL.findall(rest)) < 8:
            break
        current = rest
    current = _strip_ellipsis(current).strip().rstrip("。.")
    return re.sub(r"\s+", " ", current).strip()


def _salvage_stored(title: str) -> str:
    stem = _prepare(_strip_ellipsis(title))
    if not stem:
        return ""
    clipped = _fit(_clip_dangling(stem) if _dangling(stem) else stem)
    if clipped and _ok(clipped) and not _ends_with_ellipsis(clipped) and not title_needs_repair(clipped):
        return clipped
    return ""


def _short_snippet(text: str) -> str:
    raw = argumentative_excerpt(_plain(text), limit=80)
    piece = _prepare(raw)
    piece = _clip_dangling(piece) if _dangling(piece) else piece
    if not piece or _placeholder(piece) or _spammy(piece) or _FILLER.match(piece):
        return ""
    if len(_CJK.findall(piece)) < 2 and len(_MEANINGFUL.findall(piece)) < 4:
        return ""
    fitted = _fit(piece)
    if not fitted or _ends_with_ellipsis(fitted):
        return ""
    return fitted[:18]


def _fit(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = _strip_ellipsis(cleaned).strip().rstrip("。.")
    cleaned = cleaned.strip("，,；;、 ")
    if not cleaned:
        return ""
    if len(cleaned) <= TITLE_LIMIT:
        return _strip_ellipsis(cleaned).strip()
    window = cleaned[:TITLE_LIMIT]
    cut = -1
    for index, char in enumerate(window):
        if char in _BREAKS and index >= 8:
            cut = index
    if cut >= 8:
        fitted = window[:cut].strip()
    else:
        fitted = window.rstrip(_BREAKS + " ").strip()
    return _strip_ellipsis(fitted).strip()


def _ok(text: str) -> bool:
    if not text or _placeholder(text) or _spammy(text) or _FILLER.match(text):
        return False
    if _ends_with_ellipsis(text):
        return False
    if re.fullmatch(r"[\d\s:：./\-年月日时分秒]+", text):
        return False
    cjk = len(_CJK.findall(text))
    meaningful = len(_MEANINGFUL.findall(text))
    return cjk >= 4 or meaningful >= 8


def _dangling(text: str) -> bool:
    return _DANGLING.search(text or "") is not None


def _clip_dangling(text: str) -> str:
    match = _DANGLING.search(text or "")
    if match and match.start() >= 4:
        return text[: match.start()].strip()
    return (text or "").strip()


def _placeholder(text: str) -> bool:
    bare = re.sub(r"[\s（）()【】\[\]《》<>]", "", text or "")
    return bare in _PLACEHOLDER


def _spammy(text: str) -> bool:
    return _SPAM.search(text or "") is not None


def _ends_with_ellipsis(text: str) -> bool:
    return _ELLIPSIS_END.search(text or "") is not None


def _strip_ellipsis(text: str) -> str:
    return _ELLIPSIS_END.sub("", text or "").strip()


def _date_prefix(when: str) -> str:
    text = (when or "").strip()
    if not text or text == "日期不详":
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return ""

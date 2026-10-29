"""方伟知识星球的展示标题。

星球导出经常没有标题，或者把第一句截成「……」。卡片、文章页和重新导入时用这里的规则：

- 正文里有完整的第一句（问句也算）就用它，去掉句号，一般不超过 40 字。
- 库内标题去掉省略号后还是一句可用的话，优先于摘要，也优先于图片里的字。
- 复述摘要不能当标题。句子里出现「正文几乎」「文中未给」「作者开篇判断」，或「没有段永平本人的话」，就丢掉。
- 图片里的字只在标题是「图片」、空标题，或去掉省略号后没有可用原句时才用，不用无关的配售截图。
- 还不够，就用日期加几个字。
- 标题不能空着，不能以「…」或「...」开头或结尾，也不用「无标题」「图片」这类占位（带引号、书名号同样算）。
- 星球接口给的标题只是正文开头的十几个字，不带省略号。正文从这几个字开始、又没在这里断句，就补全成那一句。
- 标题以省略号收尾时，先去掉省略号再和正文对齐。正文里那一句不超过 40 字就整句用上；
  正文也是半截时，收到上一个逗号，不把「有两」这种没写完的半截留在标题上。
- 归档头（「- 作者：… - 时间：…」）混进标题的，重新取。
- 公司名/话题名加冒号的壳标题（「泡泡玛特：」「西部矿业：」）也要补。
  完整分句以冒号收尾（「有两点感触很深：」）不是这种壳。
- 明显错字走 textutil.fix_obvious_typos（与 present_text.polish_display_text 同一张表），
  不在这里另维护，不改观点和口语。

读者帖的「读者帖」标在作者行，不写进标题。
"""

from __future__ import annotations

import re

from researchlib.boilerplate import strip_meta
from researchlib.textutil import argumentative_excerpt, fix_obvious_typos

FANGWEI_ZSXQ = "fangwei-zsxq"
TITLE_LIMIT = 40

_ELLIPSIS_END = re.compile(
    r"(?:(?:…+|⋯+|‥+|\.{3,}|。{2,}|．{3,})[\s）)】\]」』\"'“”‘’》>]*)+$"
)
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKUP = re.compile(r"[*_`]+")
_LEAD = re.compile(
    r"^(?:"
    r"(?:方伟|作者|星主|笔者认为)(?:认为|觉得|指出|提到|说|表示|写道)[，,：:\s]*"
    r"|这篇(?:文章|短帖)?(?:认为|在讲|讨论|说的是|写的是)[，,：:\s]*"
    r"|(?:读者问|作者答|作者补充|方伟|网友)\s*[：:]\s*"
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
# 省略号在开头、结尾或句子中间，都是截断，不是一句完整的话。
_CUT = re.compile(r"…+|⋯+|‥+|\.{3,}|．{3,}|。{2,}")
_EDGE_CUT = re.compile(
    r"^(?:\s*(?:…+|⋯+|‥+|\.{3,}|．{3,}|。{2,}))+"
    r"|(?:(?:…+|⋯+|‥+|\.{3,}|．{3,}|。{2,})[\s）)】\]」』\"'“”‘’》>]*)+$"
)
# 只认开头的省略号。正文同一行「前半...后半」要把后半接回去，不能整行跳过。
_LEADING_CUT = re.compile(r"^(?:…+|⋯+|‥+|\.{3,}|．{3,}|。{2,})+")
_DATE_FALLBACK = re.compile(r"^(?:\d{4}-\d{2}-\d{2}\s+)?短帖$")
# 以前入库补标题时读到了归档头那几行，标题成了「「图片」 - 作者：方伟Rey - 时间：2026-10-19 18」。
_ARCHIVE_META = re.compile(r"(?:^|\s)-\s*(?:作者|时间|类型|topic_id)\s*[：:]")
_SENTENCE_STOP = "。！？!?；;"
_SPACES = re.compile(r"\s+")
_MISSING_NOTE = re.compile(r"正文(?:为空|没有内容|没有实质|缺失)|标题仅为|只是一张图|没有可摘的正文")
# 摘要在复述「这篇写了什么、没写什么」，不是帖子自己的那句话。
_NARRATION = re.compile(
    r"正文几乎|文中未给|作者开篇判断|没有(?:段永平|作者|他|星主)?本人的话|没有段永平的话"
)
# 识图之前写成的空壳：标题和导读是同一句「没有可读文字 / 正文为空」。
# 「正文为空洞…」这种真标题不要算进去。
_EMPTY_SHELL = re.compile(
    r"没有可读文字"
    r"|无法提炼"
    r"|没有可供概括"
    r"|没有任何(?:实质)?内容"
    r"|正文除标题"
    r"|除标题[^。\n]{0,40}外没有"
    r"|(?:本文|原文)?正文为空(?!洞)"
)
_WRAP = re.compile(r"[\s（）()【】\[\]〔〕〈〉《》<>「」『』“”‘’\"'`＂＇。！？!?…·]+")
_STOP = frozenset("的是了在和与也就都而及或被把对从到这那你我他她它中")
# 「泡泡玛特：」「西部矿业：」：整标题以中文/ASCII 冒号收尾的壳。
_SHELL_COLON = re.compile(r"^.+[：:]\s*$")


def repairs_source(source_id: str) -> bool:
    return source_id == FANGWEI_ZSXQ


def is_empty_shell(title: str) -> bool:
    """导读在正文还是空的时候写成的元标题。

    24012 的库内标题就是这一句：正文除标题「图片」外没有可读文字，无法提炼作者的核心判断及其依据。
    旧的占位表和「正文为空」正则对不上「没有可读文字」，回写会把它当成已经起好的题。
    """
    return _EMPTY_SHELL.search(title or "") is not None


def title_needs_repair(title: str, body: str = "") -> bool:
    """空标题、占位（含「图片」这种带引号的）、复述套话、空壳导读、纯话题号、带省略号，
    公司名/话题名冒号壳，明显错字，或混进了归档头。

    给了正文时，标题是正文第一句开头截下的半句也算（见 cut_from_body）。
    """
    text = (title or "").strip()
    if not text or _placeholder(text) or _narration(text) or is_empty_shell(text) or _EDGE_CUT.search(text):
        return True
    if _shell_colon(text):
        return True
    if polish_title_typos(text) != text:
        return True
    if re.fullmatch(r"\d{5,}", text):
        return True
    if _DATE_ONLY.fullmatch(text):
        return True
    if _ARCHIVE_META.search(text):
        return True
    return bool(body) and cut_from_body(text, body)


def title_needs_ai(title: str) -> bool:
    """只有空/占位/复述套话/空壳导读/省略号截断/冒号壳/归档头脏标题才值得打 flash。

    星球接口半句截断（对着正文能补全的）仍走确定性规则，避免存量几千篇全打 LLM。
    只是错字也只抛光，不起题。
    """
    text = (title or "").strip()
    if not text or _placeholder(text) or _narration(text) or is_empty_shell(text) or _EDGE_CUT.search(text):
        return True
    if _shell_colon(text):
        return True
    if re.fullmatch(r"\d{5,}", text):
        return True
    if _DATE_ONLY.fullmatch(text):
        return True
    if _ARCHIVE_META.search(text):
        return True
    return False


def polish_title_typos(title: str) -> str:
    """改正标题里的明显错字。替换表在 textutil.fix_obvious_typos，与展示层共用。"""
    return fix_obvious_typos(title or "")


def _shell_colon(title: str) -> bool:
    """整标题以冒号收尾的壳：「泡泡玛特：」「看数：」。

    中间有逗号或句号的是完整分句（「有两点感触很深：」），不是话题名。
    没有停顿、又长过公司名的，也不当壳。
    """
    text = (title or "").strip()
    if not text or not _SHELL_COLON.match(text):
        return False
    stem = text.rstrip("：:").strip()
    if not stem:
        return True
    if any(char in stem for char in "。！？!?，,；;"):
        return False
    if len(stem) > 16:
        return False
    return len(text) <= TITLE_LIMIT


def _tidy_clause_colon(text: str) -> str:
    """完整分句末尾的冒号拿掉。话题壳（「泡泡玛特：」）留着，交给补全。"""
    polished = polish_title_typos(text or "")
    if polished.endswith(("：", ":")) and not _shell_colon(polished):
        clipped = polished.rstrip("：:").strip()
        if clipped:
            return clipped
    return polished


def _without_archive_meta(title: str) -> str:
    """混进标题的归档头（「 - 作者：… - 时间：…」）切掉，留下前面的原标题。"""
    match = _ARCHIVE_META.search(title)
    return title[: match.start()].strip() if match else title


def _cut_line(title: str, body: str) -> str:
    """正文里标题截自的那一行。正文开头不是这几个字，或者这几个字后面就断句了，返回空串。

    标题自己带的省略号先去掉再对齐，否则「有两...」对不上正文里的「有两点」。
    """
    head = _SPACES.sub("", _MARKUP.sub("", _strip_ellipsis(title or "")))
    if len(head) < 4:
        return ""
    for line in _plain(strip_meta(body or "")).splitlines():
        flat = _SPACES.sub("", line)
        if not flat:
            continue
        if not flat.startswith(head):
            return ""
        rest = flat[len(head) :]
        if not rest:
            # 标题自己那一行（标题行和标题一样）：看下一行。
            continue
        peeled = _LEADING_CUT.sub("", rest, count=1).lstrip("）)】]」』\"'“”‘’》> ")
        if peeled != rest:
            # 导出的标题行「前十几个字...」。后面没字就看下一行；同一行还接着后半句就接上。
            if not peeled:
                continue
            if peeled[0] in _SENTENCE_STOP:
                return ""
            return head + peeled
        return "" if rest[0] in _SENTENCE_STOP else line
    return ""


def cut_from_body(title: str, body: str) -> bool:
    """标题是正文第一句开头截下的半句：星球接口的标题只截前十几个字，不带省略号。"""
    return bool(_cut_line(title, body))


def _completed_from_body(title: str, body: str) -> str:
    """把截断的标题补成它截自的那一句；太长时和别处一样收在逗号处。"""
    line = _cut_line(title, body)
    if not line:
        return ""
    # 问号、叹号和别处一样留着；句号、分号不留。
    sentence = re.match(f"[^{_SENTENCE_STOP}]*[{_SENTENCE_STOP}]?", line).group(0).rstrip("；;")
    fitted = _fit(_prepare(sentence))
    if not fitted or not _ok(fitted) or _contains_cut(fitted):
        return ""
    # 正文那句可能还带着错字。先按共享表抛光，再判断还要不要重抽，
    # 否则「存在运用杠杆」这类替换会把补全挡掉。
    polished = polish_title_typos(fitted)
    if not polished or title_needs_repair(polished):
        return ""
    return polished


def zsxq_display_title(
    title: str,
    *,
    body: str = "",
    summary: str = "",
    ocr: str = "",
    when: str = "",
) -> str:
    """给卡片和文章页用的标题。原标题能用就原样返回。"""
    stored = _without_archive_meta((title or "").strip())
    # 正文开头的归档头（作者、时间、点赞数）不是帖子的话，不拿来当标题。
    body = strip_meta(body or "")
    if stored and not title_needs_repair(stored, body):
        return _tidy_clause_colon(stored)
    # 只是错字：抛光后就能用，不必从正文重抽一句盖掉原标题。
    # 原标题是正文半句时不在这里返回：共享错字表一改前缀，就对不上正文。
    polished = polish_title_typos(stored)
    if (
        polished != stored
        and polished
        and not title_needs_repair(polished)
        and not cut_from_body(stored, body)
    ):
        return polished
    completed = _completed_from_body(stored, body)
    if completed:
        return polish_title_typos(completed)
    for sentence in _candidates(body):
        return polish_title_typos(sentence)
    # 去掉省略号后还是一句可用的原标题，优先于摘要和图片里的字。
    stem = _salvage_stored(stored)
    if stem:
        return polish_title_typos(stem)
    for sentence in _candidates(summary):
        return polish_title_typos(sentence)
    if _ocr_allowed(stored):
        anchors = _topic_anchors(stored)
        for sentence in _candidates(ocr):
            # 有帖子主题时，图片里的另一则新闻对不上就不拿来当标题。
            if not anchors or _overlaps(sentence, anchors):
                return polish_title_typos(sentence)
    snippet = _short_snippet(body) or _short_snippet(summary)
    if _ocr_allowed(stored):
        snippet = snippet or _short_snippet(ocr)
    date = _date_prefix(when)
    if date and snippet:
        fitted = _fit(f"{date} {snippet}")
        if fitted:
            return polish_title_typos(fitted)
    if snippet:
        return polish_title_typos(snippet)
    if date:
        return f"{date} 短帖"
    return "短帖"


def stored_zsxq_title(
    title: str,
    body: str,
    *,
    summary: str = "",
    ocr: str = "",
    when: str = "",
) -> str | None:
    """导入和回写时要写进库的标题。规则和页面上的 `zsxq_display_title` 是同一套。

    日期加「短帖」这种兜底不写入：下次打开页面还可以用摘要或图片里的字补。
    原标题本来就能用时返回 None。去掉省略号后还剩一句主干，也会写回去，避免库里继续以「…」结尾。
    """
    raw = (title or "").strip()
    if not title_needs_repair(title, body):
        return None
    # 只是错字：直接抛光写回，不必重抽一句。半句截断仍走下面的补全。
    polished = polish_title_typos(raw)
    if (
        polished != raw
        and polished
        and not title_needs_repair(polished)
        and not cut_from_body(raw, body)
    ):
        return polished
    repaired = zsxq_display_title(
        title or "",
        body=body or "",
        summary=summary or "",
        ocr=ocr or "",
        when=when or "",
    )
    if not repaired or repaired == raw:
        return None
    if title_needs_repair(repaired) or _DATE_FALLBACK.fullmatch(repaired):
        return None
    return repaired


def _drop_cut_stubs(text: str) -> str:
    """去掉「前半句...」这种标题行，免得它和下面的完整句粘成一段，整段因为省略号被丢掉。"""
    lines = (text or "").splitlines()
    flats = [_SPACES.sub("", _plain(line)) for line in lines]
    drop: set[int] = set()
    for index, line in enumerate(lines):
        if not _contains_cut(line):
            continue
        stem = _SPACES.sub("", _strip_ellipsis(_plain(line)))
        if len(stem) < 4:
            continue
        for later in flats[index + 1 : index + 6]:
            if later.startswith(stem) and len(later) > len(stem):
                drop.add(index)
                break
    if not drop:
        return text or ""
    return "\n".join(line for index, line in enumerate(lines) if index not in drop)


def _candidates(text: str) -> list[str]:
    """从一段文字里按顺序抽出能当标题的完整句。

    带省略号的半截、「正文为空」和复述摘要（正文几乎、文中未给、作者开篇判断、没有本人的话）丢掉。
    """
    found: list[str] = []
    for raw in _iter_raw(_drop_cut_stubs(text)):
        if _spammy(raw) or _contains_cut(raw) or _missing_note(raw) or _narration(raw):
            continue
        prepared = _prepare(raw)
        if _dangling(prepared):
            prepared = _clip_dangling(prepared)
        if (
            not prepared
            or _contains_cut(prepared)
            or _missing_note(prepared)
            or _narration(prepared)
            or not _ok(prepared)
        ):
            continue
        fitted = _fit(prepared)
        if not fitted or _contains_cut(fitted) or _placeholder(fitted) or _narration(fitted):
            continue
        found.append(fitted)
    return found


def _iter_raw(text: str):
    remaining = _plain(text)
    seen: set[str] = set()
    for _ in range(4):
        raw = argumentative_excerpt(remaining, limit=800)
        if not raw or raw in seen:
            return
        seen.add(raw)
        yield raw
        if raw not in remaining:
            return
        remaining = remaining.replace(raw, "\n", 1)


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
    if current.endswith(("：", ":")) and not _shell_colon(current):
        current = current.rstrip("：:").strip()
    return re.sub(r"\s+", " ", current).strip()


def _ocr_allowed(title: str) -> bool:
    """图片里的字补给空标题、占位、被省略号截断、以及没有汉字的标题。

    正文和摘要都抽不出一句时才用到。原标题去掉省略号后还剩主干的，不会走到这里。
    """
    text = (title or "").strip()
    if title_needs_repair(text):
        return True
    return _CJK.search(text) is None


def _topic_anchors(title: str) -> set[str]:
    """半截标题里的三字片段。占位标题没有主题，图片里的字可以单独当标题。"""
    stem = re.sub(r"[\s+＋]+", "", _strip_ellipsis(title or ""))
    # 占位和复述套话没有帖子主题，图片里的字可以单独当标题。
    if not stem or _placeholder(stem) or _narration(stem):
        return set()
    chars = "".join(_CJK.findall(stem))
    if len(chars) < 4:
        return set()
    anchors = set()
    for index in range(len(chars) - 2):
        gram = chars[index : index + 3]
        if sum(char in _STOP for char in gram) >= 2:
            continue
        anchors.add(gram)
    return anchors


def _overlaps(sentence: str, anchors: set[str]) -> bool:
    if not anchors:
        return True
    hay = "".join(_CJK.findall(sentence))
    return any(anchor in hay for anchor in anchors)


def _clip_ellipsis_tail(title: str, stem: str) -> str:
    """省略号说明后面没写完。半截收到上一个逗号，不留「有两点感」这种半个词。

    没有逗号（「护城河在哪里…」）就留去掉省略号的主干。本来就没有省略号的，仍只剪逗号后不到四个字的尾巴。
    """
    text = (stem or "").strip()
    if not text:
        return ""
    if not _EDGE_CUT.search(title or ""):
        return _clip_dangling(text) if _dangling(text) else text
    cut = max(text.rfind(char) for char in "，,；;")
    if cut >= 8:
        return text[:cut].strip()
    return _clip_dangling(text) if _dangling(text) else text


def _salvage_stored(title: str) -> str:
    """原标题去掉末尾省略号后，还剩一句不带截断符的话，才拿来兜底。"""
    if _placeholder(title):
        return ""
    stem = _prepare(_strip_ellipsis(title)).rstrip("+＋").strip()
    if _contains_cut(stem):
        return ""
    clipped = polish_title_typos(_fit(_clip_ellipsis_tail(title, stem)))
    if clipped and _ok(clipped) and not _contains_cut(clipped) and not title_needs_repair(clipped):
        return clipped
    return ""


def _short_snippet(text: str) -> str:
    if _narration(text):
        return ""
    raw = argumentative_excerpt(_plain(text), limit=80)
    if _narration(raw):
        return ""
    piece = _prepare(raw)
    piece = _clip_dangling(piece) if _dangling(piece) else piece
    if (
        not piece
        or _placeholder(piece)
        or _spammy(piece)
        or _FILLER.match(piece)
        or _contains_cut(raw)
        or _contains_cut(piece)
        or _narration(piece)
    ):
        return ""
    if len(_CJK.findall(piece)) < 2 and len(_MEANINGFUL.findall(piece)) < 4:
        return ""
    fitted = _fit(piece)
    if not fitted or _contains_cut(fitted):
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
    if not text or _placeholder(text) or _narration(text) or _spammy(text) or _FILLER.match(text):
        return False
    if _shell_colon(text):
        return False
    if _contains_cut(text):
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
    """「图片」、『见图』、（无标题）和裸的「图片」都是占位。"""
    bare = _WRAP.sub("", text or "")
    return bare in _PLACEHOLDER


def _missing_note(text: str) -> bool:
    return _MISSING_NOTE.search(text or "") is not None or is_empty_shell(text)


def _narration(text: str) -> bool:
    """复述摘要：在说这篇没有什么，不是帖子里的那句判断。"""
    return _NARRATION.search(text or "") is not None


def _contains_cut(text: str) -> bool:
    return _CUT.search(text or "") is not None


def _spammy(text: str) -> bool:
    return _SPAM.search(text or "") is not None


def _strip_ellipsis(text: str) -> str:
    return _ELLIPSIS_END.sub("", text or "").strip()


def _date_prefix(when: str) -> str:
    text = (when or "").strip()
    if not text or text == "日期不详":
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return ""

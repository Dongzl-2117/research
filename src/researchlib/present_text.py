"""阅读页展示层的轻量清洗：问答标题、对话标签、明显错字。

只动展示，不改库里的原文。知识星球问答导出的「# 问：…」「## 提问（…）」
「## 回答（…）」打开页面时收成「网友：」「方伟：」，和图片对话同一套。
入库新写评论时也直接用这两个标签；旧稿里的「读者问：」「作者答：」
「作者补充：」同样在这里映射。钉死的语病走 textutil.fix_obvious_typos。
不改观点和口语。
"""

from __future__ import annotations

import re

from researchlib.textutil import fix_obvious_typos

# 行首说话人：旧稿三种 → 全文统一成「网友：」「方伟：」。
_LEGACY_SPEAKER = re.compile(
    r"(?m)^(读者问|作者答|作者补充)\s*[：:]\s*"
)
_SPEAKER_MAP = {
    "读者问": "网友：",
    "作者答": "方伟：",
    "作者补充": "方伟：",
}


def normalize_dialogue_labels(text: str) -> str:
    """把评论区旧标签收成「网友：」「方伟：」。正文里已有的同款标签不动。"""

    def replace(match: re.Match[str]) -> str:
        return _SPEAKER_MAP.get(match.group(1), match.group(0))

    return _LEGACY_SPEAKER.sub(replace, text or "")


def polish_display_text(text: str) -> str:
    """展示层改正明显错字（textutil.fix_obvious_typos）。库内原文不动。"""
    return fix_obvious_typos(text or "")


# 知识星球问答导出：标题是「提问（读者 → 方伟Rey）」「回答（方伟Rey）」。
# 括号里的名字不展示。没有这对标题就不动，避免把笔记里的「回答」小节改掉。
_ASK_HEADING = re.compile(r"^提问\s*[（(].+[）)]\s*$")
_ANSWER_HEADING = re.compile(r"^回答\s*[（(].+[）)]\s*$")
_WEN_HEADING = re.compile(r"^问\s*[：:]\s*(.*\S)\s*$")
_ATX = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*)$")
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(```+|~~~+)")
_TRAILING_HASHES = re.compile(r"\s+#+\s*$")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 占有量词，不回溯（审核 I1：以前「…」×24 加几个字 3.5 秒）。每种只从一串的第一个字起试。
_ELLIPSIS_TAIL = re.compile(r"(?:(?<!…)…++|(?<!⋯)⋯++|(?<!‥)‥++|(?<!\.)\.{2,}+|(?<!。)。{2,}+|(?<!．)．{2,}+)++$")
_DANGLING_OPEN = re.compile(r"[（(\[【「『]+$")
# 短过这个的前缀不当成「问：」截断标题，免得误删真的小标题。整句相同仍删。
_DUP_PREFIX = 4


def _heading_title(raw: str) -> str:
    return _TRAILING_HASHES.sub("", (raw or "").strip())


def _split_sections(md: str) -> list[tuple[int, str, str]]:
    """按围栏外的 ATX 标题切开。(level, title, body)；level 0 是标题前的文字。"""
    sections: list[list] = [[0, "", []]]
    fence: str | None = None
    for line in md.split("\n"):
        stripped = line.strip()
        if fence:
            sections[-1][2].append(line)
            if stripped.startswith(fence):
                fence = None
            continue
        fence_match = _FENCE_OPEN.match(line)
        if fence_match:
            fence = fence_match.group(1)[:3]
            sections[-1][2].append(line)
            continue
        heading = _ATX.match(line)
        if heading:
            sections.append([len(heading.group(1)), _heading_title(heading.group(2)), []])
            continue
        sections[-1][2].append(line)
    return [(level, title, "\n".join(body)) for level, title, body in sections]


def _stem(text: str) -> str:
    """比较「问：」截断标题和提问正文用：去链接、空白、结尾省略号和没闭合的括号。"""
    flat = _MD_IMAGE.sub("", text or "")
    flat = _MD_LINK.sub(r"\1", flat)
    flat = re.sub(r"[#>*_`]+", "", flat)
    flat = re.sub(r"\s+", "", flat).casefold()
    flat = _ELLIPSIS_TAIL.sub("", flat)
    return _DANGLING_OPEN.sub("", flat)


def _duplicates_question(fragment: str, question: str) -> bool:
    """「问：」那一行是提问正文的重复或截断（带省略号也算）。"""
    head = _stem(fragment)
    body = _stem(question)
    if not head or not body:
        return False
    if head == body:
        return True
    shorter, longer = (head, body) if len(head) <= len(body) else (body, head)
    return len(shorter) >= _DUP_PREFIX and longer.startswith(shorter)


def _prefix_speaker(label: str, body: str) -> str:
    """把这一节的第一句收成「网友：…」/「方伟：…」。后面的段落不再重复标签。"""
    text = (body or "").strip("\n")
    if not text.strip():
        return ""
    lines = text.split("\n")
    index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if index is None:
        return ""
    stripped = lines[index].strip()
    if re.match(r"^(?:网友|方伟)\s*[：:]", stripped):
        return text
    structural = stripped.startswith(("![", "<")) or stripped.startswith("|") or bool(
        re.match(r"^(?:[-*+]|\d+[.)]|[>|])\s", stripped)
    )
    if structural:
        # 单独成段，避免和图片、列表粘在同一个 nl2br 段落里。
        lines.insert(index, f"{label}：")
        lines.insert(index + 1, "")
        return "\n".join(lines).strip("\n")
    lines[index] = f"{label}：{stripped}"
    return "\n".join(lines).strip("\n")


def plain_qa_headings(headings: list[str]) -> list[str]:
    """问答导出的小标题去掉括号里的名字：「提问（王小明 → 方伟Rey）」成「提问」，「回答（方伟Rey）」成「回答」。

    检索的章节名、问库的引用不带网友的昵称。和阅读页一样，提问、回答两种小标题都有才改。
    """
    if not any(_ASK_HEADING.match(item) for item in headings) or not any(
        _ANSWER_HEADING.match(item) for item in headings
    ):
        return list(headings)
    return [
        "提问" if _ASK_HEADING.match(item) else "回答" if _ANSWER_HEADING.match(item) else item for item in headings
    ]


# 问答帖归档头里的作者是提问的网友：「- 作者：王小明」。
_ARCHIVE_AUTHOR = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?作者[ \t]*[:：]")


def plain_qa_markdown(md: str) -> str:
    """问答导出的 markdown 去掉网友的昵称，给检索文字和接口的原文用。

    「## 提问（王小明 → 方伟Rey）」成「## 提问」，「## 回答（方伟Rey）」成「## 回答」；提问之前归档头那行
    「- 作者：王小明」不要。和阅读页、检索的章节名一样，提问、回答两种小标题都有才改，别的行不动。
    """
    text = md or ""
    titles = [title for level, title, _body in _split_sections(text) if level]
    if not any(_ASK_HEADING.match(title) for title in titles) or not any(
        _ANSWER_HEADING.match(title) for title in titles
    ):
        return text
    out: list[str] = []
    fence: str | None = None
    asked = False
    for line in text.split("\n"):
        if fence:
            if line.strip().startswith(fence):
                fence = None
        elif fence_match := _FENCE_OPEN.match(line):
            fence = fence_match.group(1)[:3]
        elif heading := _ATX.match(line):
            title = _heading_title(heading.group(2))
            if _ASK_HEADING.match(title):
                asked = True
                line = f"{heading.group(1)} 提问"
            elif _ANSWER_HEADING.match(title):
                line = f"{heading.group(1)} 回答"
        elif not asked and _ARCHIVE_AUTHOR.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def present_zsxq_qa(text: str) -> str:
    """知识星球问答的「提问（…）/ 回答（…）」收成对话段。

    开头若另有一行「问：」而且只是提问正文的重复或截断，丢掉，不再渲染成大标题。
    不是这种导出结构就原样返回，空白也不改。
    """
    raw = text or ""
    if "提问" not in raw or "回答" not in raw:
        return raw
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    sections = _split_sections(normalized)
    questions = [body for _level, title, body in sections if _ASK_HEADING.match(title)]
    answers = [body for _level, title, body in sections if _ANSWER_HEADING.match(title)]
    if not questions or not answers:
        return raw
    question = "\n".join(questions)
    pieces: list[str] = []
    passed_ask = False
    for level, title, body in sections:
        if level == 0:
            preamble = body.strip("\n")
            if preamble.strip():
                pieces.append(preamble)
            continue
        if _ASK_HEADING.match(title):
            passed_ask = True
            turned = _prefix_speaker("网友", body)
            if turned:
                pieces.append(turned)
            continue
        if _ANSWER_HEADING.match(title):
            turned = _prefix_speaker("方伟", body)
            if turned:
                pieces.append(turned)
            continue
        wen = _WEN_HEADING.match(title)
        if wen and not passed_ask and _duplicates_question(wen.group(1), question):
            extra = body.strip("\n")
            # 这一行下面若只是重复的提问，连着丢掉；另有内容才留下。
            if extra.strip() and not _duplicates_question(extra, question):
                pieces.append(extra)
            continue
        block = f"{'#' * level} {title}"
        rest = body.strip("\n")
        if rest:
            block = f"{block}\n\n{rest}"
        pieces.append(block)
    rewritten = "\n\n".join(pieces)
    if raw.endswith("\n"):
        rewritten += "\n"
    return rewritten


# 句读被切到下一段（或图后面那一行）的开头。开引号不算，那是下一段自己的。
_LEAD_PUNCT = re.compile(r"^[，。、；：！？,.;:!?…]+")
_ONLY_OPEN = re.compile(r"^[“\"「『‘]+$")
# 汉字、中文标点和全角符号。两行都没有的是英文，回接不碰（见 rejoin_broken_paragraphs）。
_CJK = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")
_LONE_DIGITS = re.compile(r"^\d{1,3}$")
_YEAR_LEAD = re.compile(r"^(\d+)\s*年")
_IMAGE_LINE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
_LIST_LINE = re.compile(r"^(?:[-*+]\s|\d+[.)、]\s)")
_STOP_LINE = re.compile(r"^(?:#{1,6}\s|>|\||</?[A-Za-z]|```|~~~)")
_HR_LINE = re.compile(r"^[-*_]{3,}$")


def _fence_mask(lines: list[str]) -> list[bool]:
    mask = [False] * len(lines)
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if fence:
            mask[index] = True
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            mask[index] = True
    return mask


def _is_image_line(line: str) -> bool:
    return _IMAGE_LINE.match(line.strip()) is not None


def _is_prose_line(line: str, fenced: bool) -> bool:
    if fenced:
        return False
    stripped = line.strip()
    if not stripped or _is_image_line(stripped):
        return False
    if _STOP_LINE.match(stripped) or _LIST_LINE.match(stripped) or _HR_LINE.match(stripped):
        return False
    return True


def _delete_line(lines: list[str], mask: list[bool], index: int) -> None:
    """删掉这一行。夹在两个空行中间时再收掉多出来的那一个空行。"""
    lines.pop(index)
    mask.pop(index)
    if 0 < index < len(lines) and not lines[index].strip() and not lines[index - 1].strip():
        lines.pop(index)
        mask.pop(index)


def _earlier_prose(lines: list[str], mask: list[bool], index: int) -> int | None:
    """往前找能接句读的那一行。标题、列表、代码、表格挡住；空行和图片可以跨过。"""
    cursor = index - 1
    while cursor >= 0:
        if _is_prose_line(lines[cursor], mask[cursor]):
            return cursor
        if lines[cursor].strip() and not _is_image_line(lines[cursor]):
            return None
        cursor -= 1
    return None


def _only_blanks_between(lines: list[str], earlier: int, index: int) -> bool:
    """两行之间只有空行（或紧挨着）才并成一段。隔着图片就只挪句读，字留在图后面。"""
    return all(not lines[cursor].strip() for cursor in range(earlier + 1, index))


def _next_prose(lines: list[str], mask: list[bool], index: int) -> int | None:
    cursor = index + 1
    while cursor < len(lines):
        if lines[cursor].strip() and not _is_image_line(lines[cursor]):
            if not _is_prose_line(lines[cursor], mask[cursor]):
                return None
            return cursor
        cursor += 1
    return None


def rejoin_broken_paragraphs(text: str) -> str:
    """收掉切到下一段开头的句读，拼回被拆开的年份。

    公众号按段切开（图后面常常只隔一个硬换行）时，逗号、句号会落到下一段开头，
    读起来像「，巧克力…」（文章 24040）。同一段里的硬换行，以及只隔了空行的下一段，
    整句接回去。隔着图片时，句读挪到上一段末尾，剩下的字留在图后面。单独成段的
    一到三位数字，和下一段拼得成「1988 年」这种年份，就是被拆开的首位，拼回去；
    拼不成的照原样留着：公众号常把「01」「02」单独一段当小节号，删了就丢了字。
    标题、列表、代码块、表格不跨。再跑一遍不变。

    只在中文里接。两行都没有汉字、中文标点的英文不动：Stratechery 2026 年的网页导出一个文本节点
    一行，链接字后面的「, mostly …」「. After that …」各占一行。接过去会丢掉标点后的空格
    （「before,mostly」），还改了段数：这页渲染也是译制的原文，11-04 以前的译文按没接的
    段数译，接了以后对照页对不上、退成两栏（22245：原文 61 段、译文 81 段）。
    """
    raw = text or ""
    if not raw:
        return ""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\ufffc", "")
    lines = normalized.split("\n")
    mask = _fence_mask(lines)
    dirty = normalized != raw
    index = 0
    while index < len(lines):
        if not _is_prose_line(lines[index], mask[index]):
            index += 1
            continue
        stripped = lines[index].strip()
        own = (index == 0 or not lines[index - 1].strip()) and (
            index == len(lines) - 1 or not lines[index + 1].strip()
        )
        if own and _LONE_DIGITS.fullmatch(stripped):
            nxt = index + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            if nxt < len(lines) and _is_prose_line(lines[nxt], mask[nxt]):
                year = _YEAR_LEAD.match(lines[nxt].lstrip())
                if year:
                    combined = stripped + year.group(1)
                    if re.fullmatch(r"(?:19|20)\d{2}", combined):
                        lines[nxt] = combined + lines[nxt].lstrip()[year.end(1) :]
                        _delete_line(lines, mask, index)
                        dirty = True
                        continue
            index += 1
            continue
        if _ONLY_OPEN.fullmatch(stripped):
            nxt = _next_prose(lines, mask, index)
            if nxt is not None and _CJK.search(stripped + lines[nxt]):
                lines[nxt] = stripped + lines[nxt].lstrip()
                _delete_line(lines, mask, index)
                dirty = True
                continue
            index += 1
            continue
        lead = _LEAD_PUNCT.match(stripped)
        if lead:
            earlier = _earlier_prose(lines, mask, index)
            if earlier is None:
                index += 1
                continue
            previous = lines[earlier].rstrip()
            if not _CJK.search(stripped + previous):
                index += 1
                continue
            punct = lead.group(0)
            rest = stripped[len(punct) :].lstrip()
            if punct and previous.endswith(punct):
                punct = ""
            if _only_blanks_between(lines, earlier, index):
                lines[earlier] = previous + punct + rest
                _delete_line(lines, mask, index)
            else:
                lines[earlier] = previous + punct
                if rest:
                    lines[index] = rest
                    index += 1
                else:
                    _delete_line(lines, mask, index)
            dirty = True
            continue
        index += 1
    if not dirty:
        return raw
    return "\n".join(lines)


def present_article_text(text: str) -> str:
    """正文进阅读页前：问答标题收成对话，旧标签统一，改错字，再接上被切开的句读。"""
    text = present_zsxq_qa(text or "")
    return rejoin_broken_paragraphs(polish_display_text(normalize_dialogue_labels(text)))

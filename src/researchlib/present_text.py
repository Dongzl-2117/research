"""阅读页展示层的轻量清洗：问答标题、对话标签、明显错字。

只动展示，不改库里的原文。知识星球问答导出的「# 问：…」「## 提问（…）」
「## 回答（…）」打开页面时收成「网友：」「方伟：」，和图片对话同一套。
入库新写评论时也直接用这两个标签；旧稿里的「读者问：」「作者答：」
「作者补充：」同样在这里映射。钉死的语病走 textutil.fix_obvious_typos。
要看上下文的识图误读走 vision_polish.repair_vision_misreads，不进那张表。
不改观点和口语。
"""

from __future__ import annotations

import re
import unicodedata

from researchlib.textutil import fix_obvious_typos
from researchlib.vision_polish import repair_vision_misreads

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
    """展示层改正明显错字，并校对识图误读。库内原文不动。

    语病在 textutil.fix_obvious_typos。家电语境里的「墓地」、free cash flow 的单字母滑字
    在 vision_polish，不进语病表。
    """
    return repair_vision_misreads(fix_obvious_typos(text or ""))


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
_ELLIPSIS_TAIL = re.compile(r"(?:…+|⋯+|‥+|\.{2,}|。{2,}|．{2,})+$")
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


def present_article_text(text: str) -> str:
    """正文进阅读页前：问答标题收成对话，旧标签统一，再改错字。"""
    text = present_zsxq_qa(text or "")
    return polish_display_text(normalize_dialogue_labels(text))


# 屏幕折行大约 10–24 个汉字宽。更长的是已经写完的一段，不能再往下粘。
_WRAP_MIN = 10.0
_WRAP_MAX = 24.0
_DIALOGUE_LINE = re.compile(r"^(?:方伟|网友)\s*[:：]")
_LABEL_ONLY = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9·]{1,12}[：:]$")
_SENTENCE_END = re.compile(r"[。！？!?…]$|(?:[A-Za-z])\.$")
_CONNECTOR_END = re.compile(r"[，、；：,;:]$")
_NEW_BLOCK = re.compile(r"^(?:[12]\d{3}\s*年|第[0-9一二三四五六七八九十])")
# 单独成行就是一句的短应答，不当成上一行的折行尾巴。
# 行尾是这些字时，句子多半还没说完（「在最顺的时候」）。不是错字表。
_UNFINISHED_END = frozenset("的了是在和与或而为把被对从向到着过就也都又再很最不没候时")
_STANDALONE = frozenset(
    {
        "好的",
        "收到",
        "哈哈",
        "哈哈哈",
        "嗯",
        "嗯嗯",
        "是的",
        "对",
        "谢谢",
        "感谢",
        "明白",
        "了解",
        "确实",
        "深刻反省",
    }
)
_CHAIN_MARK = re.compile(r"回复\s*@[^\s：:]{1,32}\s*[:：]|//@")
_GUEST_LINE = re.compile(r"^网友\s*[:：]\s*(?P<body>.*)$", re.S)
# 引用卡片正文以说话人起头。功能词不是人名。
_QUOTE_CARD = re.compile(r"^(?P<name>[\u4e00-\u9fff]{2,6})[：:](?P<rest>\S.{7,})", re.S)
_NOT_SPEAKER = frozenset(
    {
        "他说",
        "她说",
        "我说",
        "所以",
        "但是",
        "如果",
        "这个",
        "那个",
        "就是",
        "因为",
        "我们",
        "他们",
        "你们",
        "可以",
        "不是",
        "还是",
        "已经",
        "一个",
        "注意",
        "今天",
        "现在",
        "而且",
        "不过",
        "然后",
        "这样",
        "这种",
        "什么",
        "怎么",
        "那么",
        "自己",
        "没有",
        "不会",
        "可能",
        "应该",
        "觉得",
        "认为",
        "表示",
        "比如",
        "例如",
    }
)


def _visual_width(text: str) -> float:
    return sum(1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else 0.5 for char in text)


def _sentence_end(line: str) -> bool:
    if re.search(r"\d\.$", line):
        return False
    return _SENTENCE_END.search(line) is not None


def _join_parts(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if "一" <= left[-1] <= "鿿" and "一" <= right[0] <= "鿿":
        return left + right
    if left[-1] in "，、；：,;:" or right[0] in "，、；：,;:":
        return left + right
    return f"{left} {right}"


def _is_quote_card(body: str) -> bool:
    """「莫尼什：我是苹果用户……」这种引语卡片，不是「他说：」。"""
    match = _QUOTE_CARD.match((body or "").strip())
    if not match:
        return False
    name = match.group("name")
    if name in _NOT_SPEAKER or any(char in "的了着过和与或" for char in name):
        return False
    return True


def _is_wrap_continuation(previous: str, nxt: str) -> bool:
    """上一行是被画面截断的半句，才接到下一行。模型已经分好的段不在这里重切。"""
    if not previous or not nxt or _sentence_end(previous):
        return False
    if _DIALOGUE_LINE.match(nxt) or _NEW_BLOCK.match(nxt):
        return False
    if _is_quote_card(nxt) and not _LABEL_ONLY.fullmatch(previous):
        return False
    if _LABEL_ONLY.fullmatch(previous) or _CONNECTOR_END.search(previous):
        return True
    if nxt in _STANDALONE or _visual_width(nxt) > 6 or previous[-1] not in _UNFINISHED_END:
        return False
    # 顶到屏幕宽度、行尾还没说完、下一行又很短：才是半句话，不是模型另起的一段。
    return _WRAP_MIN <= _visual_width(previous) <= _WRAP_MAX


def merge_wrapped_utterances(text: str) -> str:
    """只把截断在半句的折行接回去。

    空行是模型分的段，保留。已经写完的一行（哪怕很短）不因为旁边也短就粘上去，
    也不把整段收成一块。「方伟：」「网友：」气泡各留一行。再跑一遍不变。
    """
    if not text or "\n" not in text:
        return text or ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized.split("\n")
    nonempty = [line.strip() for line in raw_lines if line.strip()]
    labeled = sum(1 for line in nonempty if _DIALOGUE_LINE.match(line))
    if nonempty and labeled * 2 >= len(nonempty):
        return normalized.strip("\n")
    merged: list[str] = []
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index].strip()
        if not line:
            merged.append("")
            index += 1
            continue
        if _DIALOGUE_LINE.match(line):
            merged.append(line)
            index += 1
            continue
        while index + 1 < len(raw_lines):
            nxt = raw_lines[index + 1].strip()
            if not nxt or not _is_wrap_continuation(line, nxt):
                break
            # 顶满一行只接后面那一小截。接完就停，避免整句刚好还在宽度里时把下一块标题吞进去。
            band_only = not _LABEL_ONLY.fullmatch(line) and not _CONNECTOR_END.search(line)
            line = line + nxt if _LABEL_ONLY.fullmatch(line) else _join_parts(line, nxt)
            index += 1
            if band_only:
                break
        merged.append(line)
        index += 1
    return "\n".join(merged).strip("\n")


def _guest_body(line: str) -> str | None:
    match = _GUEST_LINE.match((line or "").strip())
    if not match:
        return None
    return match.group("body").strip()


def _as_guest(line: str) -> str:
    body = _guest_body(line)
    if body is not None:
        return f"网友：{body}"
    return f"网友：{(line or '').strip()}"


def _peel_segment(part: str) -> str:
    """去掉一段雪球回复壳，留下发言。引语里的「莫尼什：」还在。"""
    text = (part or "").strip()
    stripped_at = False
    for _ in range(6):
        marked = re.match(r"^(?:回复\s*)?@\s*[^\s：:]{1,32}\s*[:：]\s*", text)
        if marked:
            text = text[marked.end() :].strip()
            stripped_at = True
            continue
        named = re.match(r"^[^\s：:]{1,32}\s*[:：]\s*((?:回复\s*)?@\s*\S.*)$", text, re.S)
        if named:
            text = named.group(1).strip()
            stripped_at = True
            continue
        break
    if not stripped_at:
        handle = re.match(r"^([^\s：:]{1,16})\s*[:：]\s*(\S.*)$", text, re.S)
        if handle and not re.search(r"[。！？!?，,、；;]", handle.group(1)):
            text = handle.group(2).strip()
    return text.strip()


def expand_reply_chain_line(line: str) -> list[str] | None:
    """一行里的「回复@ / //@」拆成从早到晚的多条。没有这种标记就返回 None。

    雪球把新回复放在最前，``//@`` 越靠后越早。这里翻成时间顺序，昵称改成「网友：」。
    """
    raw = (line or "").strip()
    if not raw or not _CHAIN_MARK.search(raw):
        return None
    host = bool(re.match(r"^方伟\s*[:：]", raw))
    body = re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", raw)
    bodies: list[str] = []
    for part in re.split(r"\s*//@\s*", body):
        peeled = _peel_segment(part)
        if peeled:
            bodies.append(peeled)
    if not bodies:
        return []
    bodies.reverse()
    lines: list[str] = []
    last = len(bodies) - 1
    for index, item in enumerate(bodies):
        speaker = "方伟：" if host and index == last else "网友："
        lines.append(f"{speaker}{item}")
    return lines


def _maybe_reverse_guest_run(run: list[str]) -> list[str]:
    """上晚下早、且末行是引用卡片时，翻成较早的发言在前。

    只碰 2–4 条全是「网友：」的短楼：第一条是较长的回复、中间有问句、最后一条以
    「人名：」开头。普通群聊、带「方伟：」的、已经是引用在前的，都不翻。
    """
    if not 2 <= len(run) <= 4:
        return run
    bodies = [_guest_body(line) or "" for line in run]
    if len(bodies[0]) < 12 or bodies[0].rstrip().endswith(("？", "?")):
        return run
    if _is_quote_card(bodies[0]) or not _is_quote_card(bodies[-1]):
        return run
    if not any(body.rstrip().endswith(("？", "?")) for body in bodies[:-1]):
        return run
    return [run[-1], *reversed(run[:-1])]


def order_nested_replies(text: str) -> str:
    """雪球 / 微博嵌套回复按时间从早到晚。

    有「回复@」「//@」就拆开并翻序；紧跟在后面的引用卡片提到这些回复之前。
    标记已经被模型拆成多条「网友：」、下面仍是引用卡片的，同样翻成时间顺序。
    没有这些结构就原样返回。再跑一遍不变。
    """
    if not text:
        return text or ""
    if "回复@" not in text and "//@" not in text and not re.search(r"(?m)^网友\s*[:：]", text):
        return text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    built: list[str] = []
    index = 0
    while index < len(lines):
        expanded = expand_reply_chain_line(lines[index])
        if expanded is None:
            built.append(lines[index])
            index += 1
            continue
        nxt = index + 1
        cards: list[str] = []
        while nxt < len(lines) and lines[nxt].strip() and len(cards) < 2:
            if expand_reply_chain_line(lines[nxt]) is not None:
                break
            body = _guest_body(lines[nxt])
            probe = body if body is not None else lines[nxt].strip()
            if not _is_quote_card(probe):
                break
            cards.append(_as_guest(lines[nxt]))
            nxt += 1
        built.extend(cards)
        built.extend(expanded)
        index = nxt
    ordered: list[str] = []
    index = 0
    while index < len(built):
        if _guest_body(built[index]) is None:
            ordered.append(built[index])
            index += 1
            continue
        nxt = index
        run: list[str] = []
        while nxt < len(built) and built[nxt].strip() and _guest_body(built[nxt]) is not None:
            run.append(built[nxt].strip())
            nxt += 1
        ordered.extend(_maybe_reverse_guest_run(run))
        index = nxt
    return "\n".join(ordered)

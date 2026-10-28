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

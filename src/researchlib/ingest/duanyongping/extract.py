"""从 poppler 的 pdftohtml XML 或 pdftotext 文本抽出段落。

第 1.0 版合集把段永平的回答加了粗体。XML 留得住粗体；纯文本只能靠「段永平：」这类署名。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from researchlib.textutil import scrub_file_page_junk

_DATE_BODY = (
    r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
    r"(?:\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?)?"
)
_DATE = re.compile(_DATE_BODY)
_DATE_ONLY = re.compile(
    rf"^(?:(?:发表于|发布时间|时间|日期)\s*[:：]?\s*)?{_DATE_BODY}\s*$"
)
_PAGE_NO = re.compile(r"^(?:[-—–]\s*)?\d{1,4}(?:\s*/\s*\d{1,4})?(?:\s*[-—–])?$")
_COLLECTION = re.compile(r"段永平博客文章合集")
_AUTHOR = re.compile(
    r"^(?:段永平|段总)(?:\s|$|[:：]|回复|说|→|->)|"
    r"^段\s*[:：]"
)
# 「回复段永平」是博友在回他，不是他本人。「段永平回复」仍是他。
_REPLY_TO_DUAN = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9_]{0,24}?\s*)?回复\s*[:：]?\s*段(?:永平|总)(?!的)"
)
_OTHER = re.compile(
    r"^(?:问|Q|q)\s*[:：]|"
    r"^(?:网友|博友|读者)(?:\s*[:：]|\S{0,16}\s*[:：]|\s)|"
    r"^.{1,24}?\s*(?:→|->)\s*.{1,24}"
)
_SPEAKER_STAMP = re.compile(r"^(.{1,24}?)\s*[（(]\s*\d{4}\s*[-/.年]")
_COMMENT_BREAK = re.compile(r"^(?:评论|网友评论|评论列表|留言)(?:\s*[（(].*[)）])?\s*$")
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold", re.I)
_WS = re.compile(r"\s+")


@dataclass
class Line:
    page: int
    top: float
    left: float
    text: str
    bold: bool
    size: float
    height: float
    page_height: float


@dataclass
class Block:
    text: str
    bold: bool
    size: float
    page: int


def normalize_date(text: str) -> str:
    """抽出第一个日期，有时分就带到秒。没有则返回空串。"""
    match = _DATE.search(text or "")
    if not match:
        return ""
    year, month, day, hour, minute, second = match.groups()
    stamp = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if hour is None:
        return stamp
    return f"{stamp} {int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"


def is_date_only(text: str) -> bool:
    return bool(_DATE_ONLY.match((text or "").strip()))


def is_reply_to_duan(text: str) -> bool:
    """博友帖或评论在回复段永平，不是段永平本人在说话。"""
    head = (text or "").strip()
    if not head or re.match(r"^(?:段永平|段总)", head):
        return False
    return bool(_REPLY_TO_DUAN.match(head))


def is_author_text(text: str) -> bool:
    head = (text or "").strip()
    if _AUTHOR.match(head):
        return True
    stamp = _SPEAKER_STAMP.match(head)
    return bool(stamp and stamp.group(1).strip() in {"段永平", "段总"})


def is_other_text(text: str) -> bool:
    head = (text or "").strip()
    if is_author_text(head):
        return False
    if is_reply_to_duan(head) or _OTHER.match(head):
        return True
    return bool(_SPEAKER_STAMP.match(head))


def is_comment_break(text: str) -> bool:
    return bool(_COMMENT_BREAK.match((text or "").strip()))


def is_chrome(text: str) -> bool:
    head = (text or "").strip()
    if not head:
        return True
    return bool(_PAGE_NO.match(head) or _COLLECTION.search(head))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _piece_text(node: ET.Element) -> tuple[str, int]:
    """元素内的文字，以及落在 <b> 里的字数。"""
    parts: list[str] = []
    bold = 0

    def add(value: str | None, is_bold: bool) -> None:
        nonlocal bold
        if not value:
            return
        parts.append(value)
        if is_bold:
            bold += len(value.strip())

    add(node.text, False)
    for child in list(node):
        child_text, child_bold = _piece_text(child)
        add(child_text, False)
        bold += child_bold
        if _local(child.tag) == "b":
            bold += len((child_text or "").strip())
        add(child.tail, False)
    return "".join(parts), bold


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except ValueError:
        return default


def parse_xml(xml: str) -> list[Line]:
    cleaned = re.sub(r"<!DOCTYPE[^>\[]*(\[[^\]]*\])?>", "", xml or "", count=1)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return []
    fonts: dict[str, tuple[float, bool]] = {}
    for node in root.iter():
        if _local(node.tag) != "fontspec":
            continue
        fonts[node.attrib.get("id", "")] = (
            _float(node.attrib.get("size"), 12),
            bool(_BOLD_FONT.search(node.attrib.get("family", ""))),
        )
    lines: list[Line] = []
    for page in root.iter():
        if _local(page.tag) != "page":
            continue
        number = int(_float(page.attrib.get("number"), len(lines) + 1))
        height = _float(page.attrib.get("height"), 800) or 800
        spans: list[Line] = []
        for node in list(page):
            if _local(node.tag) != "text":
                continue
            text, bold_chars = _piece_text(node)
            text = scrub_file_page_junk(_WS.sub(" ", text).strip())
            if not text:
                continue
            size, font_bold = fonts.get(node.attrib.get("font", ""), (12.0, False))
            plain = len(re.sub(r"\s+", "", text))
            bold = font_bold or (plain > 0 and bold_chars * 2 >= plain)
            spans.append(
                Line(
                    page=number,
                    top=_float(node.attrib.get("top")),
                    left=_float(node.attrib.get("left")),
                    text=text,
                    bold=bold,
                    size=size,
                    height=_float(node.attrib.get("height"), size or 12),
                    page_height=height,
                )
            )
        lines.extend(_spans_to_lines(spans))
    return drop_chrome(lines)


def parse_text(text: str) -> list[Line]:
    """pdftotext -layout 的输出。页与页之间是换页符。没有粗体。"""
    lines: list[Line] = []
    pages = (text or "").split("\f")
    for index, page in enumerate(pages, start=1):
        raw = [item.rstrip() for item in page.splitlines()]
        # 保留原始行号，空行才是段落间隔。压掉空行再编号会把相邻段落粘成一块。
        total = max(len(raw), 1)
        for row, item in enumerate(raw):
            if not item.strip():
                continue
            text = scrub_file_page_junk(_WS.sub(" ", item).strip())
            if not text:
                continue
            lines.append(
                Line(
                    page=index,
                    top=float(row),
                    left=float(len(item) - len(item.lstrip())),
                    text=text,
                    bold=False,
                    size=0,
                    height=1,
                    page_height=float(total),
                )
            )
    return drop_chrome(lines)


def _spans_to_lines(spans: list[Line]) -> list[Line]:
    spans.sort(key=lambda item: (item.top, item.left))
    grouped: list[list[Line]] = []
    for span in spans:
        if not grouped:
            grouped.append([span])
            continue
        prev = grouped[-1][-1]
        limit = max(2.5, (prev.height or 10) * 0.65)
        if abs(span.top - prev.top) > limit:
            grouped.append([span])
        else:
            grouped[-1].append(span)
    lines: list[Line] = []
    for group in grouped:
        group.sort(key=lambda item: item.left)
        text = _join_horizontal(group)
        if not text:
            continue
        plain = len(re.sub(r"\s+", "", text))
        bold_chars = sum(len(re.sub(r"\s+", "", item.text)) for item in group if item.bold)
        lines.append(
            Line(
                page=group[0].page,
                top=min(item.top for item in group),
                left=min(item.left for item in group),
                text=text,
                bold=plain > 0 and bold_chars * 2 >= plain,
                size=max(item.size for item in group),
                height=max(item.height for item in group) or 12,
                page_height=group[0].page_height,
            )
        )
    return lines


def _join_horizontal(spans: list[Line]) -> str:
    parts: list[str] = []
    previous_right = None
    for span in spans:
        piece = span.text.strip()
        if not piece:
            continue
        if previous_right is not None and span.left - previous_right > max(8, span.size):
            parts.append(" ")
        elif parts:
            prev = parts[-1][-1]
            nxt = piece[0]
            if prev.isascii() and prev.isalnum() and nxt.isascii() and nxt.isalnum():
                parts.append(" ")
        parts.append(piece)
        previous_right = span.left + max(len(piece) * max(span.size, 8) * 0.5, 1)
    return "".join(parts).strip()


def drop_chrome(lines: list[Line]) -> list[Line]:
    """页眉页脚：合集标题、页码，以及在页边重复出现的同一行。"""
    pages = {line.page for line in lines}
    hits: dict[str, set[int]] = {}
    for line in lines:
        key = re.sub(r"\s+", "", line.text)
        if key:
            hits.setdefault(key, set()).add(line.page)
    threshold = max(3, int(len(pages) * 0.4)) if len(pages) >= 3 else len(pages) + 1
    kept: list[Line] = []
    for line in lines:
        key = re.sub(r"\s+", "", line.text)
        margin = line.page_height > 0 and (
            line.top / line.page_height < 0.08 or line.top / line.page_height > 0.93
        )
        # 只丢掉页边反复出现的页眉页脚。「评论」这类短行在正文里，不能因为很多篇都有就删掉。
        repeated = len(hits.get(key, ())) >= threshold and margin
        if is_chrome(line.text) or repeated:
            continue
        kept.append(line)
    return kept


def join_wrapped(parts: list[str]) -> str:
    out = ""
    for part in parts:
        piece = part.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        prev = out[-1]
        nxt = piece[0]
        if prev.isascii() and prev.isalnum() and nxt.isascii() and nxt.isalnum():
            out += " " + piece
        else:
            out += piece
    return out.strip()


def _ends_sentence(text: str) -> bool:
    return bool(text) and text[-1] in "。！？!?；;"


def lines_to_blocks(lines: list[Line]) -> list[Block]:
    blocks: list[Block] = []
    buf: list[Line] = []

    def flush() -> None:
        if not buf:
            return
        text = scrub_file_page_junk(join_wrapped([item.text for item in buf]))
        if text:
            plain = len(re.sub(r"\s+", "", text))
            bold_chars = sum(len(re.sub(r"\s+", "", item.text)) for item in buf if item.bold)
            blocks.append(
                Block(
                    text=text,
                    bold=plain > 0 and bold_chars * 2 >= plain,
                    size=max(item.size for item in buf),
                    page=buf[0].page,
                )
            )
        buf.clear()

    for line in lines:
        if is_chrome(line.text):
            flush()
            continue
        if not buf:
            buf.append(line)
            continue
        prev = buf[-1]
        speaker = is_author_text(line.text) or is_other_text(line.text) or is_comment_break(line.text)
        date_boundary = is_date_only(line.text) or is_date_only(prev.text)
        size_jump = bool(line.size and prev.size and (line.size > prev.size * 1.15 or prev.size > line.size * 1.15))
        gap = line.top - prev.top
        if line.bold != prev.bold or speaker or date_boundary or size_jump:
            # 加粗的是段永平的回答，必须和前面的提问切开。
            wrapped = False
        elif line.page == prev.page:
            wrapped = gap <= max(prev.height, 1) * 1.35
        else:
            wrapped = not _ends_sentence(join_wrapped([item.text for item in buf]))
        if not wrapped:
            flush()
        buf.append(line)
    flush()
    return blocks

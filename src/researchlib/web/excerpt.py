"""超长文的节选页：从检索、问库引用点进几十万字的资料时，只显示那一段所在的章节。

林文丰飞书的资料一篇上百万字（《投资者的思索》195 万字、700 多个章节）。整页渲染要两三秒、页面五六 MB，
网站是单进程，这几秒里别的页面一起变慢；读者也只要那一节。

做法：展示用的 Markdown（和全文页同一条处理）按标题行切成章节，用分块的字定位到第几节，只渲染那一节
（太短带上后面几节，太长只取那一段前后），页面上给章节路径、上一节下一节、目录和「看全文」。
章节的切法和每节的文字按 Markdown 的哈希缓存（render_cache），每篇第一次多花几百毫秒，之后几毫秒。
不改表、不用回填。网页在线程池里并发跑，这里不放模块级的可变状态（AGENTS.md「规矩」）。
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from researchlib.textutil import markdown_to_text
from researchlib.web.quoting import _NOISE, _norm

# 原文块超过这么多（约 4 万字）的文章，带着 ?chunk= 打开时默认节选。
EXCERPT_MIN_CHUNKS = 80
# 节选不到这么多字时往后带上相邻的节，最多带到 MAX_SECTIONS 节。
MIN_WINDOW_CHARS = 1500
MAX_SECTIONS = 3
# 一节超过这么多字，只取那一段前后各 AROUND 字（按段落边界）。
MAX_SECTION_CHARS = 20000
AROUND = 4000
_PROBE = 16
_MAX_HITS = 64  # 一根探针在全文里出现超过这么多处就不用它
# 标题的尾巴（空白、闭合的 #）用 rstrip 去，不写进正则：「(\S.*?)[ \t#]*$」遇到一长串空白后面还有字是平方级。
_HEADING = re.compile(r"(#{1,6})[ \t]+(\S.*)$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Section:
    start: int
    end: int
    level: int  # 0 = 第一个标题之前的那段
    title: str
    parent: int | None  # 上一级标题所在的节


@dataclass(frozen=True)
class Outline:
    sections: tuple[Section, ...]
    sizes: tuple[int, ...]  # 每节归一后的字数
    offsets: tuple[int, ...]  # 每节文字在 joined 里从哪起
    joined: str


def split_sections(md: str) -> list[Section]:
    """按标题行切成节。代码围栏里以 # 开头的行不算标题。"""
    heads: list[tuple[int, int, str]] = []
    fence = ""
    position = 0
    for line in md.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        marker = _FENCE.match(stripped)
        if marker:
            token = marker.group(1)[0] * 3
            if not fence:
                fence = token
            elif token == fence:
                fence = ""
        elif not fence:
            match = _HEADING.match(stripped)
            if match:
                title = match.group(2).rstrip(" \t#") or match.group(2)
                heads.append((position, len(match.group(1)), markdown_to_text(title).strip()))
        position += len(line)
    sections: list[Section] = []
    starts = ([(0, 0, "")] if not heads or heads[0][0] > 0 else []) + heads
    stack: list[int] = []
    for index, (start, level, title) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(md)
        while stack and sections[stack[-1]].level >= level:
            stack.pop()
        parent = stack[-1] if stack and level else None
        sections.append(Section(start, end, level, title, parent))
        if level:
            stack.append(len(sections) - 1)
    return sections


def build_outline(md: str) -> Outline:
    sections = split_sections(md)
    texts = tuple(_norm(markdown_to_text(md[s.start:s.end])) for s in sections)
    offsets, position = [], 0
    for text in texts:
        offsets.append(position)
        position += len(text)
    return Outline(tuple(sections), tuple(len(text) for text in texts), tuple(offsets), "".join(texts))


def locate(outline: Outline, chunk_text: str, heading: str = "") -> int | None:
    """这一块在第几节：块里几根探针在各节文字里的命中数，最多的那节。章节标题对得上的同票优先。"""
    target = _NOISE.sub("", _norm(chunk_text))
    if len(target) < 8 or not outline.sections:
        return None
    if len(target) <= _PROBE:
        probes = [target]
    else:
        span = len(target) - _PROBE
        probes = [target[round(span * k / 6):round(span * k / 6) + _PROBE] for k in range(7)]
    # 每根探针按它在全文里出现几处分票：只出现一处的探针一票全给那一节，套话一样的探针几乎不算。
    votes: dict[int, float] = {}
    for probe in probes:
        hits: list[int] = []
        position = outline.joined.find(probe)
        while position >= 0 and len(hits) <= _MAX_HITS:
            hits.append(bisect_right(outline.offsets, position) - 1)
            position = outline.joined.find(probe, position + 1)
        if not hits or len(hits) > _MAX_HITS:
            continue
        for index in hits:
            votes[index] = votes.get(index, 0.0) + 1 / len(hits)
    if not votes:
        return None
    best = max(votes.values())
    if best < 1:
        return None
    leaders = [index for index, count in votes.items() if count >= best - 1e-9]
    titled = [index for index in leaders if heading and outline.sections[index].title == heading]
    return min(titled or leaders)


def path(outline: Outline, index: int) -> list[tuple[int, str]]:
    """第 index 节的章节路径：[(节号, 标题)]，从最上一级到它自己。"""
    chain = []
    current: int | None = index
    while current is not None:
        section = outline.sections[current]
        if section.title:
            chain.append((current, section.title))
        current = section.parent
    return list(reversed(chain))


@dataclass(frozen=True)
class Window:
    md: str
    first: int
    last: int
    trimmed: bool  # 一节太长，只取了一部分


def _paragraphs(md: str) -> list[tuple[int, int]]:
    """按空行切段（围栏里的空行不切），返回各段 (起, 止)。"""
    spans: list[tuple[int, int]] = []
    fence = ""
    start = 0
    position = 0
    for line in md.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)[0] * 3
            fence = "" if fence == token else (fence or token)
        if not fence and not line.strip():
            if position > start:
                spans.append((start, position))
            start = position + len(line)
        position += len(line)
    if position > start:
        spans.append((start, position))
    return spans


def _trim(md: str, chunk_text: str) -> tuple[str, bool]:
    """一节太长：只留块所在那段前后各 AROUND 字。找不到块就留开头。"""
    spans = _paragraphs(md)
    target = _NOISE.sub("", _norm(chunk_text))
    probe = target[:_PROBE] if len(target) >= _PROBE else target
    hit = 0
    for index, (start, end) in enumerate(spans):
        if probe and probe in _norm(markdown_to_text(md[start:end])):
            hit = index
            break
    low = high = hit
    while low > 0 and spans[hit][0] - spans[low - 1][0] <= AROUND:
        low -= 1
    while high + 1 < len(spans) and spans[high + 1][1] - spans[hit][1] <= AROUND:
        high += 1
    head = ""
    if low > 0 and not md[spans[low][0]:spans[low][1]].lstrip().startswith("#"):
        # 节标题留着，读者知道这是哪一节。
        first = md[spans[0][0]:spans[0][1]]
        if first.lstrip().startswith("#"):
            head = first.rstrip() + "\n\n"
    return head + md[spans[low][0]:spans[high][1]], (low > 0 or high + 1 < len(spans))


def window(md: str, outline: Outline, first: int, last: int, chunk_text: str = "") -> Window:
    """first 到 last 这几节；太短往后带，太长按块的位置截。"""
    last = max(first, min(last, first + MAX_SECTIONS - 1))
    sections = outline.sections
    while (last + 1 < len(sections) and last - first + 1 < MAX_SECTIONS
           and sum(outline.sizes[k] for k in range(first, last + 1)) < MIN_WINDOW_CHARS):
        last += 1
    text = md[sections[first].start:sections[last].end]
    if first == last and outline.sizes[first] > MAX_SECTION_CHARS and chunk_text:
        trimmed, cut = _trim(text, chunk_text)
        return Window(trimmed, first, last, cut)
    return Window(text, first, last, False)

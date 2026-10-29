"""从渲染后的 HTML 抽出可译段；图片/代码用占位符，译后再还原。

分段规则与 AIHOT ``editorial/translate.ts`` 的 ``segmentsOf`` 一致：``BLOCK`` 且没有
块级子节点才是叶子，否则继续访问子节点。因此带 ``<p>`` / 标题 / ``<li>`` 的
blockquote 会拆开，只有纯文本的短引用仍是一整段 blockquote。
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

# AIHOT segmentsOf 的 BLOCK。h1 / h6 不在里面：不是叶子，只往下看子节点。
_BLOCK = frozenset({
    "p", "h2", "h3", "h4", "h5", "li", "blockquote",
    "figcaption", "td", "th", "dt", "dd", "caption",
})
# AIHOT 的 CONTAINER。直接子节点命中才算 hasBlockChild。dl 不在里面。
_CONTAINER = re.compile(
    r"^(?:p|h[2-5]|li|blockquote|figcaption|td|th|dt|dd|caption|ul|ol|table|pre|figure|div)$"
)
# cheerio / 浏览器把这些当成空元素。<br> 若按普通开始标签加深度，后面的兄弟节点
# 会全被吞进当前块（21118 测出来只有 7 个顶层块，最后一块是整篇）。
_VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
_SKIP = frozenset({"script", "style", "pre", "code"})
_PARA_ID = re.compile(r'\bid="(p-\d+)"')
_MEDIA = re.compile(
    r"<(?:img|source|track)\b[^>]*?/?>"
    r"|<(?:picture|video)\b[^>]*>.*?</(?:picture|video)>",
    re.I | re.S,
)
_PRE_CODE = re.compile(r"<(pre|code)\b[^>]*>.*?</\1>", re.I | re.S)
_LATIN = re.compile(r"[A-Za-zÀ-ɏЀ-ӿ]")


@dataclass
class Shielded:
    html: str
    tokens: list[str]
    link_attrs: list[dict[str, str]]


def _attr_value(value: str) -> str:
    """HTMLParser 给的属性值已经还原了实体，写回去要重新转义，不然 &quot; 变回引号会把属性截断。"""
    return escape(value, quote=False).replace('"', "&quot;")


class _TopLevelCollector(HTMLParser):
    """收集文档顶层元素的 outer HTML（markdown 渲染结果几乎都是顶层块）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._depth = 0
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _VOID:
            piece = self._start(tag, attrs, close=True)
            if self._skip_depth or self._depth:
                self._buf.append(piece)
            else:
                self.blocks.append(piece)
            return
        if self._skip_depth:
            self._skip_depth += 1
            self._buf.append(self._start(tag, attrs))
            return
        if self._depth == 0 and tag.lower() in _SKIP:
            # pre/code 整块保留，不送去翻译
            self._depth = 1
            self._skip_depth = 1
            self._buf = [self._start(tag, attrs)]
            return
        if self._depth == 0:
            self._buf = [self._start(tag, attrs)]
            self._depth = 1
            return
        self._buf.append(self._start(tag, attrs))
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        piece = self._start(tag, attrs, close=True)
        if self._depth == 0:
            self.blocks.append(piece)
        else:
            self._buf.append(piece)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._buf.append(f"</{tag}>")
            self._skip_depth -= 1
            if self._skip_depth == 0:
                self.blocks.append("".join(self._buf))
                self._buf = []
                self._depth = 0
            return
        if self._depth == 0:
            return
        self._buf.append(f"</{tag}>")
        self._depth -= 1
        if self._depth == 0:
            self.blocks.append("".join(self._buf))
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buf.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._depth:
            self._buf.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._depth:
            self._buf.append(f"&#{name};")

    @staticmethod
    def _start(tag: str, attrs: list[tuple[str, str | None]], *, close: bool = False) -> str:
        parts = [f"<{tag}"]
        for key, value in attrs:
            if value is None:
                parts.append(f" {key}")
            else:
                parts.append(f' {key}="{_attr_value(value)}"')
        parts.append(" />" if close else ">")
        return "".join(parts)


def top_level_blocks(html: str) -> list[str]:
    text = (html or "").strip()
    if not text:
        return []
    parser = _TopLevelCollector()
    parser.feed(text)
    parser.close()
    if parser._buf and parser._depth:
        parser.blocks.append("".join(parser._buf))
    return [block for block in parser.blocks if block.strip()]


@dataclass(frozen=True)
class Segment:
    """一个叶子段。anchor 是自身或最近祖先的 id="p-N"（只给对照页用）。"""

    html: str
    anchor: str | None = None


class _Tx:
    def __init__(self, data: str) -> None:
        self.data = data


class _El:
    def __init__(self, name: str, start: str, *, void: bool = False) -> None:
        self.name = name
        self.start = start
        self.void = void
        self.children: list[_El | _Tx] = []
        self.replacement: str | None = None


class _FragmentParser(HTMLParser):
    """把片段解析成树。空元素不入栈；结束标签往上匹配，避免 <br> 把后面的兄弟吞进去。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _El("", "")
        self.stack: list[_El] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text()
        name = tag.lower()
        node = _El(name, raw, void=name in _VOID)
        self.stack[-1].children.append(node)
        if not node.void:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_El(tag.lower(), self.get_starttag_text(), void=True))

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _VOID:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].name == name:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(_Tx(data))

    def handle_entityref(self, name: str) -> None:
        self.stack[-1].children.append(_Tx(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.stack[-1].children.append(_Tx(f"&#{name};"))

    def handle_comment(self, data: str) -> None:
        self.stack[-1].children.append(_Tx(f"<!--{data}-->"))


def _serialize(nodes: list[_El | _Tx]) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, _Tx):
            parts.append(node.data)
            continue
        if node.replacement is not None:
            parts.append(node.replacement)
            continue
        parts.append(node.start)
        if not node.void:
            parts.append(_serialize(node.children))
            parts.append(f"</{node.name}>")
    return "".join(parts)


def _outer(node: _El) -> str:
    if node.void:
        return node.start
    return f"{node.start}{_serialize(node.children)}</{node.name}>"


def _plain_text(node: _El) -> str:
    parts: list[str] = []

    def walk(item: _El | _Tx) -> None:
        if isinstance(item, _Tx):
            parts.append(item.data)
            return
        for child in item.children:
            walk(child)

    walk(node)
    return html_lib.unescape("".join(parts))


def _has_letter(node: _El) -> bool:
    """与 AIHOT ``/\\p{L}/u`` 一样：没有字母的段（纯数字、纯图片）不送去翻译。"""
    return any(char.isalpha() for char in _plain_text(node))


def _para_id(start: str) -> str | None:
    match = _PARA_ID.search(start or "")
    return match.group(1) if match else None


def _walk(
    node: _El,
    ancestors: list[str],
    used: set[str],
    out: list[Segment],
    replacements: dict[int, str] | None,
) -> None:
    # pre / code 整段跳过，不译里面的字，和 AIHOT 一样。
    if node.name in {"pre", "code"}:
        return
    own = _para_id(node.start)
    chain = [*ancestors, own] if own else ancestors
    has_block_child = any(isinstance(child, _El) and _CONTAINER.match(child.name) for child in node.children)
    if node.name in _BLOCK and not has_block_child:
        if not _has_letter(node):
            return
        anchor = own
        if anchor is None:
            for candidate in reversed(chain):
                if candidate and candidate not in used:
                    anchor = candidate
                    break
        if anchor:
            used.add(anchor)
        out.append(Segment(_outer(node), anchor))
        if replacements is not None and (len(out) - 1) in replacements:
            node.replacement = replacements[len(out) - 1]
        return
    for child in node.children:
        if isinstance(child, _El):
            _walk(child, chain, used, out, replacements)


def _parse_and_walk(
    html: str,
    replacements: dict[int, str] | None = None,
) -> tuple[list[Segment], str]:
    parser = _FragmentParser()
    parser.feed(html or "")
    parser.close()
    found: list[Segment] = []
    for child in parser.root.children:
        if isinstance(child, _El):
            _walk(child, [], set(), found, replacements)
    return found, _serialize(parser.root.children)


def iter_segments(html: str) -> list[Segment]:
    """AIHOT segmentsOf：文档序的叶子段。没有字母的叶子不返回。"""
    if not (html or "").strip():
        return []
    found, _serialized = _parse_and_walk(html)
    return found


def segments_of(html: str) -> list[str]:
    """叶子段的 outer HTML。译制和对照都用这一份，不再把整段 blockquote 当一块。"""
    return [item.html for item in iter_segments(html)]


def _plain_len(html: str) -> int:
    return len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip())


def coarse_segment_reason(html: str, *, recorded_segments: int | None = None) -> str | None:
    """译文还是旧的粗分段时返回原因，否则 None。

    正确写回之后，大引用的外壳还在，但每个叶子只是一段。这里只抓两种过时结构：
    一个叶子自己仍包着很多段（没按 segmentsOf 拆开），或库里记下的段数远少于
    现在能切出的叶子。只占全文两成左右的定义列表引用不会被当成要重译。
    """
    text = html or ""
    if not text.strip():
        return None
    leaves = segments_of(text)
    lengths = [_plain_len(leaf) for leaf in leaves]
    total = sum(lengths)
    for leaf, length in zip(leaves, lengths, strict=True):
        inner = len(re.findall(r"<(?:p|h[1-6]|li)\b", leaf, re.I))
        dominates = total > 0 and length >= 8_000 and length / total >= 0.5
        if inner >= 8 or (dominates and inner >= 4):
            return "giant-leaf"
    if (
        recorded_segments is not None
        and recorded_segments > 0
        and len(leaves) >= 12
        and recorded_segments * 3 < len(leaves)
        and len(leaves) - recorded_segments >= 15
    ):
        return "segments-below-leaves"
    return None


def splice_segments(html: str, replacements: dict[int, str]) -> str:
    """按 segments_of 的下标，把译文写回原来的叶子，外层 blockquote / 列表 / 表格不动。"""
    if not html:
        return ""
    _found, serialized = _parse_and_walk(html, replacements)
    return serialized


def translatable_blocks(html: str) -> list[tuple[int, str]]:
    """返回 (segments_of 下标, 段 HTML)。跳过无拉丁字母的段（纯中文/符号/图）。"""
    out: list[tuple[int, str]] = []
    for index, segment in enumerate(iter_segments(html)):
        plain = re.sub(r"<[^>]+>", "", segment.html)
        if not _LATIN.search(plain):
            continue
        if re.fullmatch(r"\s*", plain):
            continue
        out.append((index, segment.html))
    return out


def shield(html: str) -> Shielded:
    tokens: list[str] = []

    def media_or_code(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"⟦{len(tokens) - 1}⟧"

    cleaned = _PRE_CODE.sub(media_or_code, html)
    cleaned = _MEDIA.sub(media_or_code, cleaned)
    link_attrs: list[dict[str, str]] = []

    def link_repl(match: re.Match[str]) -> str:
        attrs_raw = match.group(1) or ""
        inner = match.group(2)
        attrs: dict[str, str] = {}
        for am in re.finditer(
            r"([^\s=]+)(?:\s*=\s*\"([^\"]*)\"|\s*=\s*'([^']*)'|\s*=\s*([^\s>]+))?",
            attrs_raw,
        ):
            key = am.group(1)
            value = am.group(2) or am.group(3) or am.group(4) or ""
            attrs[key] = value
        link_attrs.append(attrs)
        return f'<a id="L{len(link_attrs) - 1}">{inner}</a>'

    cleaned = re.sub(r"<a\b([^>]*)>(.*?)</a>", link_repl, cleaned, flags=re.I | re.S)
    return Shielded(html=cleaned, tokens=tokens, link_attrs=link_attrs)


def unshield(translated: str, shielded: Shielded) -> str | None:
    counts: dict[int, int] = {}
    for match in re.finditer(r"⟦(\d+)⟧", translated):
        index = int(match.group(1))
        counts[index] = counts.get(index, 0) + 1
    if len(counts) != len(shielded.tokens):
        return None
    if any(counts.get(i) != 1 for i in range(len(shielded.tokens))):
        return None

    def put_token(match: re.Match[str]) -> str:
        return shielded.tokens[int(match.group(1))]

    text = re.sub(r"⟦(\d+)⟧", put_token, translated)
    seen: set[int] = set()

    def put_link(match: re.Match[str]) -> str:
        index = int(match.group(1))
        inner = match.group(2)
        if index in seen or index >= len(shielded.link_attrs):
            return match.group(0)
        seen.add(index)
        attrs = shielded.link_attrs[index]
        parts = ["<a"]
        for key, value in attrs.items():
            if key.lower() == "id":
                continue
            parts.append(f' {key}="{value}"')
        parts.append(f">{inner}</a>")
        return "".join(parts)

    text = re.sub(r'<a\s+id="L(\d+)">(.*?)</a>', put_link, text, flags=re.I | re.S)
    if len(seen) != len(shielded.link_attrs):
        return None
    return text


def chunk_segments(
    parts: list[str],
    limit: int = 60_000,
    *,
    max_segments: int | None = None,
) -> list[list[str]]:
    """按字符上限分批。max_segments 给定时，一批最多这么多段。

    单段比 limit 还长时单独成批，不从中间切开（切开就对不齐原文块）。
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for part in parts:
        over_chars = bool(current) and size + len(part) > limit
        over_count = max_segments is not None and bool(current) and len(current) >= max_segments
        if over_chars or over_count:
            chunks.append(current)
            current = []
            size = 0
        current.append(part)
        size += len(part)
    if current:
        chunks.append(current)
    return chunks

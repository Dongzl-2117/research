"""从渲染后的 HTML 抽出可译顶层块；图片/代码用占位符，译后再还原。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

# 与 AIHOT segmentsOf 的 BLOCK 集合对齐：这些是叶子文本块标签。
_LEAF = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "figcaption", "td", "th", "dt", "dd", "caption",
})
_SKIP = frozenset({"script", "style", "pre", "code"})
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


def translatable_blocks(html: str) -> list[tuple[int, str]]:
    """返回 (原 top-level 下标, 块 HTML)。跳过无拉丁字母的块（纯中文/符号/图）。"""
    out: list[tuple[int, str]] = []
    for index, block in enumerate(top_level_blocks(html)):
        plain = re.sub(r"<[^>]+>", "", block)
        if not _LATIN.search(plain):
            continue
        if re.fullmatch(r"\s*", plain):
            continue
        out.append((index, block))
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

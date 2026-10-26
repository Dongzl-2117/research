"""文章页：把引用的那一块在正文里找出来，标上高亮，打开时滚到那里。

问库的 [n]、检索结果、观点原句都链到 /articles/{id}?chunk={块号}。原来落在文章顶部，再靠顶上的
「引用的段落」框自己往下找；现在在渲染好的正文里按文字找到这一块覆盖的段落，加上 is-quoted。
按渲染后的 HTML 找，不预先存段落编号：图片后来下载到本地、只有图的段落编号会变，存下的编号就对不上了。
找不到（比如块里的字和渲染出的段落差太多）就还用顶上的框。
"""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser

from researchlib.citecheck import quote_spans
from researchlib.translate.segments import top_level_blocks

QUOTED_CLASS = "is-quoted"
_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_OPEN = re.compile(r"^<([a-z0-9]+)\b([^>]*)>", re.I)
_CLASS = re.compile(r'\bclass="([^"]*)"', re.I)


def _norm(text: str) -> str:
    return "".join(_KEEP.findall(html_lib.unescape(text or ""))).lower()


def _add_class(block: str) -> str:
    match = _OPEN.match(block)
    if not match:
        return f'<div class="{QUOTED_CLASS}">{block}</div>'
    tag, attrs = match.group(1), match.group(2)
    if _CLASS.search(attrs):
        if QUOTED_CLASS in _CLASS.search(attrs).group(1).split():
            return block
        attrs = _CLASS.sub(lambda m: f'class="{m.group(1)} {QUOTED_CLASS}"', attrs, count=1)
    else:
        attrs = f'{attrs} class="{QUOTED_CLASS}"'
    return f"<{tag}{attrs}>{block[match.end():]}"


def _block_text(block: str) -> str:
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

        def handle_starttag(self, tag, attrs):
            if tag in {"br", "hr", "p", "li", "div", "blockquote", "td", "th"}:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            self.handle_starttag(tag, [])

    parser = Text()
    parser.feed(block)
    parser.close()
    text = "".join(parser.parts).strip()
    if re.fullmatch(r"\[(?:外部图片已省略|[^\]\n]* 缺图)\]", text):
        return ""
    return text


def mark_quoted(html: str, chunk_text: str) -> tuple[str, bool]:
    """返回 (标好的 HTML, 找到没有)。"""
    target = _norm(chunk_text)
    if len(target) < 8:
        return html, False
    blocks = top_level_blocks(html)
    if not blocks:
        return html, False
    texts = [_block_text(block) for block in blocks]
    source = "\n".join(texts)
    spans = quote_spans(source, chunk_text)
    if spans is None:
        return html, False
    out = list(blocks)
    offset = 0
    for index, text in enumerate(texts):
        end = offset + len(text)
        if text and any(start < end and stop > offset for start, stop in spans):
            out[index] = _add_class(blocks[index])
        offset = end + 1
    return "".join(out), True

"""导出时一条引文、一个列表项写了两遍：单看原文时收成一份。

Stratechery 的归档导出先把整条引文、整个列表项写成一行纯文字（链接和加粗都没了，
链接后面的逗号前多一个空格，脚注末尾的「↩」也不是链接），紧接着把里面的每一段
带着格式再写一遍::

    > A note on these numbers.

    A note on these numbers.

    - Search: The first point … the ads business , …（里面两段连成一行）

    **Search:** The first point … the [ads business](https://example.com/ads), …

    The second paragraph of the same item …

    - Kind of like this ↩

    Kind of like this [↩](#rf2-1000)

页面上每段引文、每条带段落的列表项和每条脚注都出现两次。这里认紧跟在引文、列表项
后面的一到几段普通段落：去掉格式、标点和空白后接起来正好等于外层那份，就把它们
放回引文、列表项里（带着链接和加粗），纯文字那份不再显示。差一个字都不动。

只在单看原文时用：文章页没有译文、或者点了「原文」，以及 /api/v1/articles/{id}.md。
中英对照按位置配对，译文是照着带重复的原文译的，原文收掉一份就和译文错开，所以对照
照旧，长的重复段由对照页自己收（translate/pair.py）。译制、检索和问库读的也是原样，
检索按段去重（retrieval 的 _distinct_passages）。库里的 body_md 不改。
"""

from __future__ import annotations

import html as html_lib
import re

from researchlib.textutil import markdown_to_text

_FENCE = re.compile(r"^[ \t]{0,3}(```+|~~~+)")
_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?(.*)$")
# 外层的列表项只认顶格写的：缩进的多半是嵌套列表，不动。
_ITEM = re.compile(r"^(?:[-*+]|\d{1,9}[.)])[ \t]+(?=\S)")
_ANY_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\S")
# 不能当副本的行：引文、列表、标题、表格、HTML、缩进四格的代码、分隔线和 setext 标题的下划线。
_NOT_PLAIN = re.compile(
    r"^(?:[ \t]{0,3}(?:>|#{1,6}(?:[ \t]|$)|[-*+][ \t]|\d{1,9}[.)][ \t]|\||<)|[ \t]{4}|\t)"
    r"|^[ \t]{0,3}(?:(?:[-*_][ \t]*){3,}|=+)[ \t]*$"
)
_NON_KEY = re.compile(r"[^0-9a-z一-鿿]+")
# 外层去掉格式、标点和空白后短过这个就不认：「Yes.」这种短句碰巧和下一段一样，不当成导出的两份。
_MIN_KEY = 8
# 一条引文、一个列表项里最多认这么多段。
_MAX_COPIES = 12
# 列表项里第二段起要缩进四格才还在这一项里（Python-Markdown 的规矩）。
_INDENT = "    "


def _key(md: str) -> str:
    text = html_lib.unescape(markdown_to_text(md)).casefold()
    return _NON_KEY.sub("", text)


def markdown_blocks(lines: list[str]) -> list[tuple[int, int, bool]]:
    """按空行切块：[(起, 止, 含不含围栏代码)]，止不含。围栏代码里的空行不切。"""
    found: list[tuple[int, int, bool]] = []
    start: int | None = None
    fence = ""
    code = False
    for index, line in enumerate(lines):
        if fence:
            if line.lstrip().startswith(fence):
                fence = ""
            continue
        opened = _FENCE.match(line)
        if opened:
            fence = opened.group(1)[:3]
            code = True
            if start is None:
                start = index
            continue
        if line.strip():
            if start is None:
                start = index
            continue
        if start is not None:
            found.append((start, index, code))
            start, code = None, False
    if start is not None:
        found.append((start, len(lines), code))
    return found


def _container(block: list[str]) -> tuple[str, int, str, str] | None:
    """引文或列表项：(种类, 从块里第几行起, 列表标记, 外层的文字)。不是就返回 None。

    一块里有好几个列表项时只看最后一项：导出的副本紧跟在它自己那一项后面。
    引文里还套着引文、列表的，列表项下面还挂着嵌套列表的，都不动。
    """
    quoted = [_QUOTE.match(line) for line in block]
    if all(quoted):
        inner = [match.group(1) for match in quoted if match]
        if any(_QUOTE.match(text) or _ANY_ITEM.match(text) or _FENCE.match(text) for text in inner):
            return None
        return "quote", 0, "", "\n".join(inner)
    for offset in range(len(block) - 1, -1, -1):
        marker = _ITEM.match(block[offset])
        if marker is None:
            continue
        rest = block[offset + 1 :]
        if any(_QUOTE.match(line) or _ANY_ITEM.match(line) or _FENCE.match(line) for line in rest):
            return None
        text = "\n".join([block[offset][marker.end() :], *(line.strip() for line in rest)])
        return "item", offset, marker.group(0), text
    return None


def _plain(block: list[str]) -> bool:
    return not any(_NOT_PLAIN.match(line) for line in block)


def _as_quote(paragraphs: list[list[str]]) -> list[str]:
    out: list[str] = []
    for number, paragraph in enumerate(paragraphs):
        if number:
            out.append(">")
        out.extend(f"> {line.strip()}" for line in paragraph)
    return out


def _as_item(marker: str, paragraphs: list[list[str]]) -> list[str]:
    out: list[str] = []
    for number, paragraph in enumerate(paragraphs):
        if number:
            out.append("")
        for row, line in enumerate(paragraph):
            out.append((marker if number == 0 and row == 0 else _INDENT) + line.strip())
    return out


def fold_nested_copies(md: str) -> str:
    """把紧跟在引文、列表项后面、和它一字不差的带格式副本放回去，纯文字那份不再显示。没有就原样返回。"""
    if not md or ("\n" not in md):
        return md or ""
    lines = md.split("\n")
    blocks = markdown_blocks(lines)
    edits: list[tuple[int, int, list[str]]] = []
    index = 0
    while index < len(blocks):
        start, end, code = blocks[index]
        found = None if code else _container(lines[start:end])
        target = _key(found[3]) if found else ""
        if found is None or len(target) < _MIN_KEY:
            index += 1
            continue
        kind, offset, marker, _text = found
        joined = ""
        copies: list[int] = []
        for following in range(index + 1, min(len(blocks), index + 1 + _MAX_COPIES)):
            copy_start, copy_end, copy_code = blocks[following]
            if copy_code or not _plain(lines[copy_start:copy_end]):
                break
            piece = _key("\n".join(lines[copy_start:copy_end]))
            if not piece or not target.startswith(joined + piece):
                break
            joined += piece
            copies.append(following)
            if joined == target:
                break
        if not copies or joined != target:
            index += 1
            continue
        paragraphs = [lines[blocks[number][0] : blocks[number][1]] for number in copies]
        rebuilt = _as_quote(paragraphs) if kind == "quote" else _as_item(marker, paragraphs)
        edits.append((start + offset, blocks[copies[-1]][1], rebuilt))
        index = copies[-1] + 1
    if not edits:
        return md
    out: list[str] = []
    cursor = 0
    for begin, finish, rebuilt in edits:
        out.extend(lines[cursor:begin])
        out.extend(rebuilt)
        cursor = finish
    out.extend(lines[cursor:])
    return "\n".join(out)

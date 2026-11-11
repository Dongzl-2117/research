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
好几段的引文，纯文字那份也分段（段间是「>」空行），整条引文算一个外层。
引文里的图在纯文字那份里没有，带格式那几段中间夹着（``media``：只有图、或者只剩一个
没字的链接；只有「[…]」这种标点的段落也算）；夹在中间的跟着带格式那几段放回去，不算字。
原文的引文、列表项里还套着引文、列表的，导出只写一遍，夹在带格式那几段中间（``nested``），
按整块的字接，一起放回去。引文里套着的引文自己也写了两遍时，先收里面那条，外层下一轮才接得上。
外层开头写着一句导语（列表项里的「某某解释说：」）、或者结尾写着署名（推文的「— 某某 (@handle) 日期」），
带格式那份不再写这一截：后面几块接上外层中间连着的一截（至少 ``_MIN_INSIDE`` 个字），多出来的只有开头
或者只有结尾，也收（``_inside_run``）。多出来的那截照纯文字留在外层里，带格式那几段接在它后面或者前面
（``plain_ends`` 找切开的位置）。图的替代文字不算字：纯文字那份里没有。

导出还常把同一张图连着写两遍（原文把图包在链接里时尤其多），页面上也只留一张
（``drop_repeated_images``）。

这里收 markdown，只在单看原文时用：文章页没有译文、或者点了「原文」，以及
/api/v1/articles/{id}.md。译文是照着带重复的原文译的，原文先收掉一份就和译文错开，
所以中英对照和只看中文在按位置配好以后，用同一条规则（``copy_runs``）在页面上收
（translate/display.py 的 ``_fold_copy_pairs``、``fold_translated_copies``）。
译制、检索和问库读的是原样，检索按段去重（retrieval 的 _distinct_passages）。库里的 body_md 不改。
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

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
# 一条引文、一个列表项里最多认这么多段。访谈记录这类长引文一条几十段（线上 22699 一条 40 多段），以前只认 12 段，
# 整段记录在三种页面上都显示两遍。接得上要逐字相等，段数多了也不会认错，只是多比几段。
_MAX_COPIES = 400
# 外层开头、结尾多出来、导出没再写带格式那份的字（导语、推文署名）：中间接上的那一截至少这么多字才认，
# 一句话以上。短了容易碰上正文自己把引文里的半句话又写一遍。
_MIN_INSIDE = 30
_CONTAINERS = frozenset({"quote", "item"})
# 只有图、或者只剩一个没字的链接的块：导出写带格式那几段时夹在中间，纯文字那份里没有它们。
_IMAGE_MD = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_EMPTY_LINK_MD = re.compile(r"\[\s*\]\([^)\n]*\)")
# 列表项里第二段起要缩进四格才还在这一项里（Python-Markdown 的规矩）。
_INDENT = "    "


def _key(md: str) -> str:
    # 图的替代文字不算字：纯文字那份里没有，页面上（translate/display.py 的 repeat_key）也不算。
    text = html_lib.unescape(markdown_to_text(_IMAGE_MD.sub(" ", md))).casefold()
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


def _media(block: list[str]) -> bool:
    """只有图、或者只剩没字的链接（原文的图包在链接里，导出把链接和图拆开写）的普通块。"""
    text = "\n".join(block)
    if not _plain(block) or not (_IMAGE_MD.search(text) or _EMPTY_LINK_MD.search(text)):
        return False
    return not _NON_KEY.sub("", _EMPTY_LINK_MD.sub("", _IMAGE_MD.sub("", text)).casefold())


def _image_urls(block: list[str]) -> list[str]:
    return [match.group(0).rsplit("](", 1)[1].split()[0].rstrip(")") for match in _IMAGE_MD.finditer("\n".join(block))]


def _drop_repeated_images(lines: list[str]) -> list[str]:
    """导出把同一张图连着写了两遍：只有图的块和紧挨着的上一块是同几张图，这一块不再显示。"""
    blocks = markdown_blocks(lines)
    drop: list[tuple[int, int]] = []
    previous: list[str] = []
    last_end = 0
    for start, end, code in blocks:
        block = lines[start:end]
        urls = _image_urls(block) if not code and _media(block) else []
        if urls and urls == previous:
            # 连同前面的空行一起拿掉，和上一块之间不留两个空行。
            drop.append((last_end, end))
        else:
            previous = urls
            last_end = end
    if not drop:
        return lines
    out: list[str] = []
    cursor = 0
    for begin, finish in drop:
        out.extend(lines[cursor:begin])
        cursor = finish
    out.extend(lines[cursor:])
    return out


def _lines_of(paragraph: list[str]) -> list[str]:
    """放回引文、列表项时每行的写法：普通段落去掉行首空白；套着的引文、列表留着自己的缩进（嵌套靠它）。"""
    if _plain(paragraph):
        return [line.strip() for line in paragraph]
    return [line.rstrip() for line in paragraph]


def _as_quote(paragraphs: list[list[str]]) -> list[str]:
    out: list[str] = []
    for number, paragraph in enumerate(paragraphs):
        if number:
            out.append(">")
        out.extend(f"> {line}".rstrip() for line in _lines_of(paragraph))
    return out


def _as_item(marker: str, paragraphs: list[list[str]]) -> list[str]:
    out: list[str] = []
    for number, paragraph in enumerate(paragraphs):
        if number:
            out.append("")
        for row, line in enumerate(_lines_of(paragraph)):
            out.append((marker if number == 0 and row == 0 else _INDENT) + line)
    return out


# 带格式那几段里还能有这些块：引文里套着的引文、列表（导出只写一遍，按整块的字算）。
_NESTED = frozenset({"quote", "item", "nested"})


class CopyRun(NamedTuple):
    """``copy_runs`` 认出的一处：外层那几块、后面带格式那几块的下标，外层开头、结尾没有副本的字数。"""

    outers: list[int]
    copies: list[int]
    lead: int = 0
    trail: int = 0


def _piece(kinds: Sequence[str], keys: Sequence[str], whole: Sequence[str], number: int) -> str:
    kind = kinds[number]
    return keys[number] if kind == "plain" else whole[number] if kind in _NESTED else ""


def _chain(
    kinds: Sequence[str], keys: Sequence[str], whole: Sequence[str], last: int, target: str,
) -> tuple[list[int], int]:
    """外层（到 ``last`` 为止）后面接得上 ``target`` 开头的那几块：(这几块, 接上了几个字)。"""
    joined = ""
    copies: list[int] = []
    # 图先记着，后面还有接得上的段落才算进副本。
    images: list[int] = []
    texts = 0
    for following in range(last + 1, len(kinds)):
        if kinds[following] == "media":
            if len(images) >= _MAX_COPIES:
                break
            images.append(following)
            continue
        piece = _piece(kinds, keys, whole, following)
        if not piece or not target.startswith(joined + piece) or texts >= _MAX_COPIES:
            break
        joined += piece
        copies.extend(images)
        images = []
        copies.append(following)
        texts += 1
        if joined == target:
            break
    return copies, len(joined)


def _copies_after(
    kinds: Sequence[str], keys: Sequence[str], whole: Sequence[str], last: int, target: str,
) -> list[int]:
    """外层（到 ``last`` 为止）后面接起来正好等于 ``target`` 的那几块；接不上就是空的。"""
    copies, length = _chain(kinds, keys, whole, last, target)
    return copies if copies and length == len(target) else []


def _inside_run(kinds: Sequence[str], keys: Sequence[str], whole: Sequence[str], index: int) -> CopyRun | None:
    """外层开头多一截导语、或者结尾多一截署名，只有中间那截后面有带格式的副本。

    原文的列表项里直接写着一句导语（「某某解释说：」），下面套着引文；推文的引文里正文是一段，
    署名（「— 某某 (@handle) 日期」）直接写在引文里。导出把整条写成一行纯文字，带格式那份只写里面的段落，
    导语、署名不再写。后面紧跟着的几块要正好接成外层中间连着的一截（至少 ``_MIN_INSIDE`` 个字），
    开头或者结尾多出来的字只能有一头：两头都多的是正文把引文中间一句又写了一遍，不收。
    """
    first = next((number for number in range(index + 1, len(kinds)) if kinds[number] != "media"), None)
    piece = _piece(kinds, keys, whole, first) if first is not None else ""
    target = keys[index]
    if not piece or len(target) <= _MIN_INSIDE:
        return None
    start = target.find(piece)
    while start >= 0:
        copies, length = _chain(kinds, keys, whole, index, target[start:])
        rest = len(target) - start - length
        after = next((number for number in range(copies[-1] + 1, len(kinds)) if kinds[number] != "media"), None) \
            if copies else None
        # 署名正好是下一块的开头：那是嵌套列表的项和正文后面的项并在了一个列表里（只看中文页先拆开列表再认），
        # 不是署名。
        spilled = bool(rest) and after is not None and _piece(kinds, keys, whole, after).startswith(target[-rest:])
        if copies and length >= _MIN_INSIDE and (start == 0) != (rest == 0) and not spilled:
            return CopyRun([index], copies, start, rest)
        start = target.find(piece, start + 1)
    return None


def copy_runs(
    kinds: Sequence[str], keys: Sequence[str], whole: Sequence[str] | None = None, *, quote_parts: bool = False,
) -> list[CopyRun]:
    """一串块里认导出写的两份：[CopyRun(外层那几块的下标, 后面带格式那几块的下标, 导语字数, 署名字数)]。

    ``kinds`` 是每块的种类：``"quote"``、``"item"`` 能当外层（引文、列表项），``"plain"`` 是普通段落，
    ``"media"`` 是没有字的块（图、「[…]」），``"nested"`` 是别的引文、列表（不能当外层），别的都不算。
    ``keys`` 是去掉格式、标点和空白后的文字（列表块是最后一项的），``whole`` 是整块的（不给就用 ``keys``）。

    外层短过 ``_MIN_KEY`` 不认；紧跟着的几块接起来正好等于外层才算，差一个字都不动，最多认 ``_MAX_COPIES``
    段。带格式那几块是普通段落，或者引文里套着的引文、列表（按整块的字算）；夹在中间（或者第一段前面）的图
    算进副本，不占字，最后一段后面的图不算。带格式那几块里的引文、列表自己也写了两遍（后面跟着它自己的
    副本）时，这一轮先收里面那条，外层等下一轮。``quote_parts``：对照页上好几段的引文一段一对，连着的几对
    引文合起来当一个外层（先试最长的）。一块的外层正好接不上时，再看开头的导语、结尾的署名（``_inside_run``）：
    ``lead``、``trail`` 是外层的字里开头、结尾没有副本的字数，这些字收的时候留在外层里。原文页（这里的
    markdown）、对照页和只看中文页（translate/display.py）都按这一条认。
    """
    whole = keys if whole is None else whole
    found = {
        index: run for index in range(len(kinds))
        if (run := _run_at(kinds, keys, whole, index, quote_parts=quote_parts))
    }
    runs: list[CopyRun] = []
    index = 0
    while index < len(kinds):
        run = found.get(index)
        if run and not any(number in found for number in run.copies):
            runs.append(run)
            index = run.copies[-1] + 1
        else:
            index += 1
    return runs


def _run_at(
    kinds: Sequence[str], keys: Sequence[str], whole: Sequence[str], index: int, *, quote_parts: bool,
) -> CopyRun | None:
    if kinds[index] not in _CONTAINERS:
        return None
    last = index
    if quote_parts and kinds[index] == "quote":
        while last + 1 < len(kinds) and kinds[last + 1] == "quote":
            last += 1
    for end in range(last, index - 1, -1):
        target = "".join(keys[number] for number in range(index, end + 1))
        copies = _copies_after(kinds, keys, whole, end, target) if len(target) >= _MIN_KEY else []
        if copies:
            return CopyRun(list(range(index, end + 1)), copies)
    return _inside_run(kinds, keys, whole, index)


_SPACE = re.compile(r"\s+")


def plain_ends(
    text: str, target: str, lead: int, trail: int, key: Callable[[str], str], cuts: Sequence[int] | None = None,
) -> tuple[int, int] | None:
    """外层那份纯文字按 ``CopyRun`` 的导语、署名切开：返回 (导语在哪儿结束, 署名从哪儿开始)，没有的那头是 0、
    ``len(text)``。

    在空白处切（``cuts`` 给了就只在这些位置切）。导语取最长的：末尾的冒号、破折号跟着导语；署名取最长的：
    开头的破折号跟着署名。切出来的两头和中间那截各自去掉格式后要正好是 ``target`` 的三段，不然是 None。
    """
    points = list(cuts) if cuts is not None else [match.start() for match in _SPACE.finditer(text)]
    middle = target[lead:len(target) - trail]
    head_at, tail_at = 0, len(text)
    if lead:
        found = None
        for point in points:
            got = key(text[:point])
            if got == target[:lead]:
                found = point
            elif len(got) > lead:
                break
        if found is None:
            return None
        head_at = found
    if trail:
        found = None
        for point in reversed(points):
            got = key(text[point:])
            if got == target[len(target) - trail:]:
                found = point
            elif len(got) > trail:
                break
        if found is None or found < head_at:
            return None
        tail_at = found
    if key(text[head_at:tail_at]) != middle:
        return None
    return head_at, tail_at


# 整块的字：去掉每行开头的引文「>」和列表标记（有序列表的序号不算字，页面上的纯文字那份里也没有）。
_LINE_MARKS = re.compile(r"^[ \t]*(?:>[ \t]?)*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?")


def _whole_key(block: list[str]) -> str:
    return _key("\n".join(_LINE_MARKS.sub("", line) for line in block))


def _fold_once(lines: list[str], done: set[int]) -> tuple[list[str], set[int]] | None:
    """收一轮：(收过的行, 放回去的引文、列表项各从第几行起)。``done`` 是前几轮放回去的：它们只能当别的外层
    带格式那几块里的一块，不再当外层（不然正文自己接着写的同一段会被下一轮收进去）。"""
    blocks = markdown_blocks(lines)
    found: list[tuple[str, int, str, str] | None] = []
    kinds: list[str] = []
    keys: list[str] = []
    whole: list[str] = []
    for start, end, code in blocks:
        block = lines[start:end]
        item = None if code else _container(block)
        found.append(item)
        key = ""
        if item and any(start <= line < end for line in done):
            kind = "nested"
        elif item:
            kind = item[0]
            key = _key(item[3])
        elif code:
            kind = ""
        elif _media(block):
            kind = "media"
        elif _plain(block):
            key = _key("\n".join(block))
            # 「[…]」这种只有标点的段落和图一样夹在带格式那几段中间，不占字。
            kind = "plain" if key else "media"
        else:
            # 引文里套着引文、列表项下面挂着嵌套列表的：不当外层，可以是别的外层带格式那几块里的一块。
            kind = "nested" if _QUOTE.match(block[0]) or _ANY_ITEM.match(block[0]) else ""
        kinds.append(kind)
        keys.append(key)
        whole.append(_whole_key(block) if kind else "")
    edits: list[tuple[int, int, list[str]]] = []
    for run in copy_runs(kinds, keys, whole):
        index = run.outers[0]
        container = found[index]
        if container is None:
            continue
        kind, offset, marker, text = container
        paragraphs = [lines[blocks[number][0] : blocks[number][1]] for number in run.copies]
        if run.lead or run.trail:
            cut = plain_ends(text, keys[index], run.lead, run.trail, _key)
            if cut is None:
                continue
            head, tail = text[:cut[0]].strip(), text[cut[1]:].strip()
            paragraphs = [*([head.split("\n")] if head else []), *paragraphs, *([tail.split("\n")] if tail else [])]
        rebuilt = _as_quote(paragraphs) if kind == "quote" else _as_item(marker, paragraphs)
        edits.append((blocks[index][0] + offset, blocks[run.copies[-1]][1], rebuilt))
    if not edits:
        return None
    out: list[str] = []
    placed: set[int] = set()
    cursor = 0
    shift = 0
    for begin, finish, rebuilt in edits:
        # 前几轮放回去的、这一轮没动的，跟着前面删掉的行挪位置。
        placed.update(line + shift for line in done if cursor <= line < begin)
        out.extend(lines[cursor:begin])
        placed.add(len(out))
        out.extend(rebuilt)
        shift += len(rebuilt) - (finish - begin)
        cursor = finish
    placed.update(line + shift for line in done if line >= cursor)
    out.extend(lines[cursor:])
    return out, placed


def fold_nested_copies(md: str) -> str:
    """把紧跟在引文、列表项后面、和它一字不差的带格式副本放回去，纯文字那份不再显示；连着写两遍的同一张图
    只留一张。没有就原样返回。

    引文里套着的引文自己也写了两遍时，外层要等里面那条收好才接得上：最多收三轮。
    """
    if not md or ("\n" not in md):
        return md or ""
    original = md.split("\n")
    lines = _drop_repeated_images(original)
    done: set[int] = set()
    for _round in range(3):
        folded = _fold_once(lines, done)
        if folded is None:
            break
        lines, done = folded
    return md if lines is original else "\n".join(lines)

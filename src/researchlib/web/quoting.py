"""文章页：把引用的那一块在正文里找出来，标上高亮，打开时滚到那里。

问库的 [n]、检索结果、观点原句都链到 /articles/{id}?chunk={块号}。原来落在文章顶部，再靠顶上的
「引用的段落」框自己往下找；现在在渲染好的正文里按文字找到这一块覆盖的段落，加上 is-quoted。
按渲染后的 HTML 找，不预先存段落编号：图片后来下载到本地、只有图的段落编号会变，存下的编号就对不上了。
找不到（比如块里的字和渲染出的段落差太多）就还用顶上的框。
"""

from __future__ import annotations

import html as html_lib
import re

from researchlib.translate.segments import top_level_blocks

QUOTED_CLASS = "is-quoted"
_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_TAG = re.compile(r"<[^>]+>")
_OPEN = re.compile(r"^<([a-z0-9]+)\b([^>]*)>", re.I)
_CLASS = re.compile(r'\bclass="([^"]*)"', re.I)
_KEY = 24
_SLACK = 8


def _norm(text: str) -> str:
    return "".join(_KEEP.findall(html_lib.unescape(text or ""))).lower()


def _add_class(block: str) -> str:
    match = _OPEN.match(block)
    if not match:
        return f'<div class="{QUOTED_CLASS}">{block}</div>'
    tag, attrs = match.group(1), match.group(2)
    if _CLASS.search(attrs):
        attrs = _CLASS.sub(lambda m: f'class="{m.group(1)} {QUOTED_CLASS}"', attrs, count=1)
    else:
        attrs = f'{attrs} class="{QUOTED_CLASS}"'
    return f"<{tag}{attrs}>{block[match.end():]}"


def _reach(text: str, target: str, position: int) -> int | None:
    """这一段接在块里 position 处，接上以后吃到哪。接不上返回 None。"""
    rest = target[position:]
    if text.startswith(rest[:_KEY]) and len(text) >= len(rest):
        return len(target)  # 块在这一段中间结束
    # 容几个字的出入：有序列表的序号、标点之类在两边的写法不一样。
    found = target.find(text, position, position + len(text) + _SLACK)
    if found < 0 or found - position > _SLACK:
        return None
    return found + len(text)


def _follow(blocks: list[str], norms: list[str], target: str, start: int, consumed: int) -> tuple[list[int], int]:
    """从 start 这一段顺着往下吃：后面的段落文字接得上块里剩下的字就算上，直到块吃完。返回 (段落, 吃到哪)。"""
    covered = [start]
    position = consumed
    for index in range(start + 1, len(blocks)):
        if position >= len(target):
            break
        text = norms[index]
        if not text or "<img" in blocks[index] or text.endswith("缺图") or text == "外部图片已省略":
            continue  # 只有图（或缺图占位）的段落：块里没有它的字，跳过，接着往下看
        reached = _reach(text, target, position)
        if reached is None:
            # 导出时写了两遍的引文、列表项，页面上收成了一份（nested_copies），块里还是两遍：
            # 紧接着又是刚标上的那段时，跳过这一遍再接。
            last = norms[covered[-1]]
            if len(last) >= _KEY and target.startswith(last, position):
                reached = _reach(text, target, position + len(last))
        if reached is None:
            break
        covered.append(index)
        position = reached
    return covered, position


def mark_quoted(html: str, chunk_text: str) -> tuple[str, bool]:
    """返回 (标好的 HTML, 找到没有)。"""
    target = _norm(chunk_text)
    if len(target) < 8:
        return html, False
    blocks = top_level_blocks(html)
    if not blocks:
        return html, False
    norms = [_norm(_TAG.sub("", block)) for block in blocks]
    head = target[:_KEY]
    for index, text in enumerate(norms):
        if len(text) < 4:
            continue
        # 这一段就是块的开头，或者块从这一段中间开始（长段落被切成了几块）。
        if target.startswith(text[:_KEY]):
            consumed = len(text)
        else:
            found = text.find(head)
            if found < 0:
                continue
            consumed = len(text) - found
        covered, position = _follow(blocks, norms, target, index, consumed)
        if consumed < _KEY and len(covered) == 1 and position < len(target):
            # 很短的一段碰巧和块的开头一样（小标题、一句话），后面又接不上：不是这里，接着找。
            continue
        out = list(blocks)
        for block in covered:
            out[block] = _add_class(blocks[block])
        return "".join(out), True
    return html, False

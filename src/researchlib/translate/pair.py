"""原始叶子的配对工具；页面必须经 display.article_pairs 的完整性和映射检查。

不单边去重、不用最小段数差证明对应。只有两侧完整文字、资源与结构都相同的整对可折叠。
旧稿切句前后的选择仍供兼容展示使用，无法证明逐段对应时由展示层明确保留完整两侧。
"""

from __future__ import annotations

import html as html_lib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from researchlib.translate.segments import (
    Segment,
    iter_segments,
    top_level_blocks,
)

_MEDIA_TAGS = re.compile(
    r"<(?:img|source|track)\b[^>]*?/?>"
    r"|<(?:picture|video)\b[^>]*>.*?</(?:picture|video)>",
    re.I | re.S,
)


@dataclass(frozen=True)
class BilingualPair:
    en: str
    zh: str | None
    anchor: str | None = None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _plain(html: str) -> str:
    return _norm(re.sub(r"<[^>]+>", "", html or ""))


def _take_anchor(html: str) -> tuple[str, str | None]:
    match = re.match(r"^<([a-z0-9]+)\b([^>]*)>", html or "", re.I)
    if not match:
        return html, None
    attrs = match.group(2)
    id_match = re.search(r'\bid="(p-\d+)"', attrs)
    if not id_match:
        return html, None
    anchor = id_match.group(1)
    cleaned_attrs = re.sub(r'\s*id="p-\d+"', "", attrs)
    rebuilt = f"<{match.group(1)}{cleaned_attrs}>{html[match.end():]}"
    return rebuilt, anchor


def stamp_paragraph_ids(html: str) -> str:
    blocks = top_level_blocks(html)
    if not blocks:
        return html
    out: list[str] = []
    n = 0
    for block in blocks:
        plain = _plain(block)
        if not plain:
            out.append(block)
            continue
        n += 1
        want = f"p-{n}"
        match = re.match(r"^<([a-z0-9]+)\b([^>]*)>", block, re.I)
        if not match:
            out.append(f'<div id="{want}">{block}</div>')
            continue
        tag, attrs = match.group(1), match.group(2)
        if re.search(rf'\bid="{re.escape(want)}"', attrs):
            out.append(block)
            continue
        existing = re.search(r'\bid="([^"]+)"', attrs)
        body = block[match.end() :]
        if existing and not re.match(r"p-\d+$", existing.group(1)):
            attrs = re.sub(r'\s*id="[^"]+"', "", attrs)
            out.append(f'<{tag} id="{want}"{attrs}><span id="{existing.group(1)}" class="para-anchor"></span>{body}')
        else:
            attrs = re.sub(r'\s*id="p-\d+"', "", attrs)
            out.append(f'<{tag} id="{want}"{attrs}>{body}')
    return "".join(out)


def _one_pair(en_html: str, o_text: str, zh_raw: str, zh_text: str, anchor: str | None) -> BilingualPair:
    """一段对照。译文为空、或纯文本和原文相同，就不显示中文。图片只留在英文侧。"""
    from researchlib.translate.integrity import SegmentFacts, repeat_identity

    if not zh_text or repeat_identity(en_html) == repeat_identity(zh_raw):
        return BilingualPair(en=en_html, zh=None, anchor=anchor)
    zh = zh_raw
    if re.search(r"<(img|picture|video|source)\b", zh, re.I):
        shared = Counter(SegmentFacts(en_html).resources)

        def remove_shared(match):
            resources = Counter(SegmentFacts(match.group()).resources)
            if resources and not (resources - shared):
                shared.subtract(resources)
                return ""
            return match.group()

        zh = _MEDIA_TAGS.sub(remove_shared, zh)
        if not zh.strip():
            return BilingualPair(en=en_html, zh=None, anchor=anchor)
    return BilingualPair(en=en_html, zh=zh, anchor=anchor)


def _gap(left: Sequence[object], right: Sequence[object]) -> int:
    return abs(len(left) - len(right))


def _entries(html: str, *, split_br: bool = True, split_sentences: bool = True) -> list[Segment]:
    return iter_segments(html, split_br=split_br, split_sentences=split_sentences)


def _contained_text(html: str) -> str:
    """可见文字，只压空白并忽略大小写。标点留着，括号不同的两条来源注释不会变成包含。

    标签换成空格再压空白。``<strong>`` 紧挨句号时，和正文里句号前多一个空格是同一长度。
    """
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _display_long(html: str) -> bool:
    """对照页上够不够大约 80 字。和 ``repeat_long`` 同一道门槛，但标签边界按空格计。"""
    return len(_contained_text(html)) >= 80


def visible_text(html: str) -> str:
    """对照页上看得见的文字：去标签、压空白、忽略大小写。质检和近重复判断用同一份。"""
    return _contained_text(html)


def _show(segment: Segment) -> tuple[str, str | None]:
    """去掉段上的段落号。叶子本身没有 id 时，用切段时记下的祖先 id。"""
    html, anchor = _take_anchor(segment.html)
    return html, anchor or segment.anchor


def _best_blocks(
    original_html: str, zh_html: str, *, split_br: bool, split_sentences: bool, dedupe: bool = True
) -> tuple[list[Segment], list[Segment]] | None:
    """只切叶子；数量不齐时绝不各自去重来凑出相等条数。"""
    kids_o = _entries(original_html, split_br=split_br, split_sentences=split_sentences)
    kids_t = _entries(zh_html, split_br=split_br, split_sentences=split_sentences)
    if not kids_o and not kids_t:
        return None
    if dedupe and len(kids_o) == len(kids_t):
        drop = set(redundant_pairs(pairs_from_blocks(kids_o, kids_t)))
        return ([item for index, item in enumerate(kids_o) if index not in drop],
                [item for index, item in enumerate(kids_t) if index not in drop])
    return kids_o, kids_t


def _blocks_for_pairs(
    original_html: str, zh_html: str, *, dedupe: bool = True
) -> tuple[list[Segment], list[Segment]] | None:
    """选展示用的叶子段。两边都没有叶子段才返回 None。

    超长多句先按句切开，普通段落里的 ``<br>`` 也按句收成段。只在收完后两边的差额
    不再变大时才用这一份。旧译文是把整段当一段译的，拆开只会让对照从那句起错开，
    这时仍整段一对。新译文写回时叶子数一致，走拆开的那一份。
    ``dedupe=False`` 交给 ``_best_blocks``：只切段，不删任何内容。
    """
    split = _best_blocks(original_html, zh_html, split_br=True, split_sentences=True, dedupe=dedupe)
    if split is None or _gap(*split) == 0:
        return split
    coarse = _best_blocks(
        original_html, zh_html, split_br=False, split_sentences=False, dedupe=dedupe
    )
    if coarse is None or _gap(*coarse) >= _gap(*split):
        return split
    return coarse


def redundant_pairs(pairs: Sequence[BilingualPair]) -> list[int]:
    """只有两侧完整文字、资源归属及结构都相同，才允许整对折叠。"""
    from researchlib.translate.integrity import repeat_identity

    seen: set[tuple] = set()
    drop = []
    identities = []
    for index, pair in enumerate(pairs):
        identity = (repeat_identity(pair.en), repeat_identity(pair.zh or ""))
        if identity in seen and (_display_long(pair.en) or (index and identities[-1] == identity)):
            drop.append(index)
        else:
            seen.add(identity)
        identities.append(identity)
    return drop


def display_blocks(
    original_html: str, zh_html: str, *, dedupe: bool = True
) -> tuple[list[Segment], list[Segment]] | None:
    """展示用的两边叶子。质检读同一份，避免再写一套切叶规则。

    ``dedupe=False`` 只切叶子，不给展示层删内容：投影检查拿这一份当基准。
    """
    return _blocks_for_pairs(original_html, zh_html, dedupe=dedupe)


def pairs_from_blocks(kids_o: Sequence[Segment], kids_t: Sequence[Segment]) -> list[BilingualPair]:
    """把已经切好的两边叶子按下标配成对照。展示层先切好叶子时用这一份，不重复切。"""
    pairs: list[BilingualPair] = []
    n = min(len(kids_o), len(kids_t))
    for i in range(n):
        en_html, _en_anchor = _show(kids_o[i])
        zh_raw, anchor = _show(kids_t[i])
        pairs.append(_one_pair(en_html, _plain(kids_o[i].html), zh_raw, _plain(zh_raw), anchor))
    for i in range(n, len(kids_o)):
        en_html, _en_anchor = _take_anchor(kids_o[i].html)
        pairs.append(BilingualPair(en=en_html, zh=None, anchor=None))
    for i in range(n, len(kids_t)):
        zh_raw, anchor = _show(kids_t[i])
        pairs.append(_one_pair("", "", zh_raw, _plain(zh_raw), anchor))
    return pairs


def bilingual_pairs(
    original_html: str, zh_html: str, *, dedupe: bool = True
) -> list[BilingualPair] | None:
    """按位置把叶子段配成对照。译过就出对照，不因为段数差返回 None。

    选中的两边里，下标对得上的并排。原文更长时，多出来的段 zh 为 None。
    译文更长时，多出来的段 en 为空，单独接在后面。长度等于较长的一边。
    ``dedupe=False`` 用未去重的叶子：展示层先看完整细对的投影，再自己收重复。
    """
    blocks = _blocks_for_pairs(original_html, zh_html, dedupe=dedupe)
    if blocks is None:
        return None
    return pairs_from_blocks(blocks[0], blocks[1])

"""把原文 HTML 与译文 HTML 配成中英对照。

新译文按叶子段原位写回，和 AIHOT segmentsOf 一样：blockquote 里还有块级子节点时，
段是里面的 p / 标题 / li，不是整段引用。多条或很长的定义列表引用会再拆进 dt / dd。
这里按段的位置并排。
旧稿若只在中文侧删过连续重复段，两边再走一遍同一套去重，把同段拉齐。
叶子数已经一样时也走这一遍：两边都变短且仍然一样长，才收掉引用后的重复段。
段数仍对不齐也不整页退回中文：多出来的挂在末尾。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from researchlib.translate.segments import (
    Segment,
    iter_segments,
    repeat_key,
    repeat_long,
    same_repeat,
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


def _strip_media(html: str) -> str:
    return _MEDIA_TAGS.sub("", html or "").strip()


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
    if not zh_text or zh_text == o_text:
        return BilingualPair(en=en_html, zh=None, anchor=anchor)
    zh = zh_raw
    if re.search(r"<(img|picture|video|source)\b", zh, re.I):
        zh = _strip_media(zh)
        if not _plain(zh) or _plain(zh) == o_text:
            return BilingualPair(en=en_html, zh=None, anchor=anchor)
    return BilingualPair(en=en_html, zh=zh, anchor=anchor)


def _gap(left: Sequence[object], right: Sequence[object]) -> int:
    return abs(len(left) - len(right))


def _entries(html: str, *, split_br: bool = True, split_sentences: bool = True) -> list[Segment]:
    return iter_segments(html, split_br=split_br, split_sentences=split_sentences)


def _leaf_key(html: str) -> str:
    """去标签、空白和标点。列表项和紧跟着的同文段落、域名两侧多出来的空格因此对得上。"""
    return repeat_key(html)


def _drop_repeated_leaves(segments: list[Segment]) -> list[Segment]:
    """同一套规则去掉已经出现过的长叶子。

    全文相同的后一份丢掉（引用整段再贴一遍，或隔了几段又出现）。紧挨着的后一份如果只是
    前一份的开头（引用里用一个 ``<br>`` 粘了两段，复述只重复了第一段），也丢掉。
    只共享开头、后文不同的两段都留着。
    """
    priors: dict[str, list[str]] = {}
    kept: list[Segment] = []
    for segment in segments:
        key = _leaf_key(segment.html)
        if repeat_long(segment.html):
            bucket = key[:48]
            if any(same_repeat(earlier, key) for earlier in priors.get(bucket, ())):
                continue
            priors.setdefault(bucket, []).append(key)
        kept.append(segment)
    return kept


def _show(segment: Segment) -> tuple[str, str | None]:
    """去掉段上的段落号。叶子本身没有 id 时，用切段时记下的祖先 id。"""
    html, anchor = _take_anchor(segment.html)
    return html, anchor or segment.anchor


def _best_blocks(
    original_html: str, zh_html: str, *, split_br: bool, split_sentences: bool
) -> tuple[list[Segment], list[Segment]] | None:
    """按一种切法选两边展示用的叶子。先原样，再看同一套去重。

    连续相同的块，以及纯文本已经出现过的长叶子（列表项和紧跟的同文段落、后文再贴的
    同一段），两边各跑一遍。叶子数已经一样时，只有两边都变短并且仍然一样长，才改用
    去重后的段。只短一边会把长度拉开，仍用未去重的等长序列，不单边丢掉，也不硬配。
    叶子数本来就不一样时，只有去重后差距变小才改用那一份。
    """
    kids_o = _entries(original_html, split_br=split_br, split_sentences=split_sentences)
    kids_t = _entries(zh_html, split_br=split_br, split_sentences=split_sentences)
    if not kids_o and not kids_t:
        return None
    raw_gap = _gap(kids_o, kids_t)
    # 懒加载：页面导入 pair 时不必立刻拉起翻译服务。叶子数已经一样也要跑。
    from researchlib.translate.service import _dedupe_consecutive_blocks

    block_o = _entries(
        _dedupe_consecutive_blocks(original_html),
        split_br=split_br,
        split_sentences=split_sentences,
    )
    block_t = _entries(
        _dedupe_consecutive_blocks(zh_html),
        split_br=split_br,
        split_sentences=split_sentences,
    )
    candidates = (
        (block_o, block_t),
        (_drop_repeated_leaves(kids_o), _drop_repeated_leaves(kids_t)),
        (_drop_repeated_leaves(block_o), _drop_repeated_leaves(block_t)),
    )
    best: tuple[int, int, list[Segment], list[Segment]] | None = None
    for left, right in candidates:
        if not left or not right:
            continue
        gap = _gap(left, right)
        equal_shrink = (
            raw_gap == 0
            and gap == 0
            and len(left) == len(right)
            and len(left) < len(kids_o)
            and len(right) < len(kids_t)
        )
        if not equal_shrink and gap >= raw_gap:
            continue
        if best is None or gap < best[0] or (gap == best[0] == 0 and len(left) < best[1]):
            best = (gap, len(left), left, right)
    if best is None:
        return kids_o, kids_t
    return best[2], best[3]


def _blocks_for_pairs(original_html: str, zh_html: str) -> tuple[list[Segment], list[Segment]] | None:
    """选展示用的叶子段。两边都没有叶子段才返回 None。

    超长多句先按句切开，普通段落里的 ``<br>`` 也按句收成段。只在收完后两边的差额
    不再变大时才用这一份。旧译文是把整段当一段译的，拆开只会让对照从那句起错开，
    这时仍整段一对。新译文写回时叶子数一致，走拆开的那一份。
    """
    split = _best_blocks(original_html, zh_html, split_br=True, split_sentences=True)
    if split is None or _gap(*split) == 0:
        return split
    coarse = _best_blocks(original_html, zh_html, split_br=False, split_sentences=False)
    if coarse is None or _gap(*coarse) >= _gap(*split):
        return split
    return coarse


def bilingual_pairs(original_html: str, zh_html: str) -> list[BilingualPair] | None:
    """按位置把叶子段配成对照。译过就出对照，不因为段数差返回 None。

    选中的两边里，下标对得上的并排。原文更长时，多出来的段 zh 为 None。
    译文更长时，多出来的段 en 为空，单独接在后面。长度等于较长的一边。
    """
    blocks = _blocks_for_pairs(original_html, zh_html)
    if blocks is None:
        return None
    kids_o, kids_t = blocks
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

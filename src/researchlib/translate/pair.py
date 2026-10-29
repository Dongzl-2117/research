"""把原文 HTML 与译文 HTML 配成中英对照。

新译文按叶子段原位写回，和 AIHOT segmentsOf 一样：blockquote 里还有块级子节点时，
段是里面的 p / 标题 / li，不是整段引用。多条或很长的定义列表引用会再拆进 dt / dd。
这里按段的位置并排。
旧稿若只在中文侧删过连续重复段，两边再走一遍同一套去重，把同段拉齐。
段数仍对不齐也不整页退回中文：多出来的挂在末尾。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from researchlib.translate.segments import Segment, iter_segments, top_level_blocks

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


def _gap(left: list[object], right: list[object]) -> int:
    return abs(len(left) - len(right))


def _entries(html: str) -> list[Segment]:
    return iter_segments(html)


def _show(segment: Segment) -> tuple[str, str | None]:
    """去掉段上的段落号。叶子本身没有 id 时，用切段时记下的祖先 id。"""
    html, anchor = _take_anchor(segment.html)
    return html, anchor or segment.anchor


def _blocks_for_pairs(original_html: str, zh_html: str) -> tuple[list[Segment], list[Segment]] | None:
    """结构一致就按原样配。旧稿段数对不齐时，用同一套去重把同段拉齐。

    译文入库曾经只删中文侧的连续重复段，原文还留着引用后的那一段。展示时两边
    再跑一遍；比较忽略 id="p-N"。差距变小才改用去重后的段，变小之后仍有差额也
    按拉齐后的序列配，不退回未去重的错位拼接。两边都没有叶子段才返回 None。
    """
    kids_o = _entries(original_html)
    kids_t = _entries(zh_html)
    if not kids_o and not kids_t:
        return None
    if kids_o and kids_t and _gap(kids_o, kids_t) == 0:
        return kids_o, kids_t
    # 懒加载：盖段落号不必为了相等的结构去拉翻译服务。
    from researchlib.translate.service import _dedupe_consecutive_blocks

    deduped_o = _entries(_dedupe_consecutive_blocks(original_html))
    deduped_t = _entries(_dedupe_consecutive_blocks(zh_html))
    if deduped_o and deduped_t and _gap(deduped_o, deduped_t) < _gap(kids_o, kids_t):
        return deduped_o, deduped_t
    return kids_o, kids_t


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

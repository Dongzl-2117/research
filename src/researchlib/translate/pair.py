"""把原文 HTML 与译文 HTML 按顶层块配成中英对照（与 AIHOT bilingualPairs 一致）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from researchlib.translate.segments import top_level_blocks

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


def _gap(left: list[str], right: list[str]) -> int:
    return abs(len(left) - len(right))


def _blocks_for_pairs(original_html: str, zh_html: str) -> tuple[list[str], list[str]] | None:
    """两边顶层块一样多就按原样配。

    译文入库时去掉了连续相同的段，原文没有。展示时又盖了 p-id，字符串不再相同，
    块数会差一截：差一块以上整页退回纯中文，只差一块则后面的段错位。
    两边再走一遍同一套去重；差距缩小并且仍在差一块以内，才改用去重后的块。
    块数本来一样的不动，免得只削掉英文、把译文仍保留的那一段错开。
    """
    kids_o = top_level_blocks(original_html)
    kids_t = top_level_blocks(zh_html)
    raw_gap = _gap(kids_o, kids_t)
    if kids_o and raw_gap == 0:
        return kids_o, kids_t
    # 懒加载：stamp / mdlinks 会进来，不该为了盖段落号去拉翻译服务。
    from researchlib.translate.service import _dedupe_consecutive_blocks

    deduped_o = top_level_blocks(_dedupe_consecutive_blocks(original_html))
    deduped_t = top_level_blocks(_dedupe_consecutive_blocks(zh_html))
    deduped_gap = _gap(deduped_o, deduped_t)
    if deduped_o and deduped_t and deduped_gap <= 1 and deduped_gap < raw_gap:
        return deduped_o, deduped_t
    if kids_o and raw_gap <= 1:
        return kids_o, kids_t
    return None


def bilingual_pairs(original_html: str, zh_html: str) -> list[BilingualPair] | None:
    blocks = _blocks_for_pairs(original_html, zh_html)
    if blocks is None:
        return None
    kids_o, kids_t = blocks
    pairs: list[BilingualPair] = []
    n = min(len(kids_o), len(kids_t))
    for i in range(n):
        en_html, _ = _take_anchor(kids_o[i])
        zh_raw, anchor = _take_anchor(kids_t[i])
        o_text = _plain(kids_o[i])
        zh_text = _plain(zh_raw)
        if not zh_text or zh_text == o_text:
            pairs.append(BilingualPair(en=en_html, zh=None, anchor=anchor))
            continue
        zh = zh_raw
        if re.search(r"<(img|picture|video|source)\b", zh, re.I):
            zh = _strip_media(zh)
            if not _plain(zh) or _plain(zh) == o_text:
                pairs.append(BilingualPair(en=en_html, zh=None, anchor=anchor))
                continue
        pairs.append(BilingualPair(en=en_html, zh=zh, anchor=anchor))
    for i in range(n, len(kids_o)):
        en_html, _ = _take_anchor(kids_o[i])
        pairs.append(BilingualPair(en=en_html, zh=None, anchor=None))
    return pairs

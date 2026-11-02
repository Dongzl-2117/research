"""有序叶子的兼容结构接口。

不再单侧删段、收近前缀或切回粗粒度来凑段数；不能对应时不给位置候选。
完整页面使用 display.article_pairs，读取持久回执并单独报告旧稿不确定性。
redundant_pairs 仅用于提示可能重复的位置，不授权删除任何源文或译文。
"""

from __future__ import annotations

import difflib
import html as html_lib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from researchlib.translate.segments import (
    Segment,
    iter_segments,
    repeat_key,
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
    repeat_of: int | None = None


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
    """兼容叶子入口也保留独有译文资源，共享媒体只隐藏对应的那一份。"""
    from dataclasses import replace

    from researchlib.translate.display import _whole_pair

    return replace(_whole_pair(en_html, zh_raw), anchor=anchor)


def _gap(left: Sequence[object], right: Sequence[object]) -> int:
    return abs(len(left) - len(right))


def _entries(html: str, *, split_br: bool = True, split_sentences: bool = True) -> list[Segment]:
    return iter_segments(html, split_br=split_br, split_sentences=split_sentences)


def _leaf_key(html: str) -> str:
    """去标签、空白和标点。列表项和紧跟着的同文段落、域名两侧多出来的空格因此对得上。"""
    return repeat_key(html)


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


def _same_lineage(earlier_zh: str, later_zh: str) -> bool:
    """两句中文是不是同一段的译文，包括措辞不同的那一种。

    完全相同、互为开头、几乎一样，或后一句是空的，沿用原来的规则。分开译的两遍
    常常只共享中间一长串（大约 12 个字以上），也算同一段。对不上这一串的是另一句。
    """
    if _translation_repeats(earlier_zh, later_zh):
        return True
    if not earlier_zh or not later_zh:
        return False
    match = difflib.SequenceMatcher(None, earlier_zh, later_zh, autojunk=False).find_longest_match(
        0, len(earlier_zh), 0, len(later_zh)
    )
    return match.size >= 12


def _translation_repeats(earlier_zh: str, later_zh: str) -> bool:
    """后一对的中文是不是前面那一对的同一段译文。

    不看 80 字。中文通常比英文短，同一句译文往往不到这个长度。后一对没有译文时，
    留下它也不会多出内容。两边都空，或去掉标点后相同、只是前面那段的开头、几乎一样，都算。
    """
    if not later_zh:
        return True
    if not earlier_zh:
        return False
    return same_repeat(earlier_zh, later_zh)


def _shared_prefix_len(left: str, right: str) -> int:
    count = 0
    for char_left, char_right in zip(left, right, strict=False):
        if char_left != char_right:
            break
        count += 1
    return count


def _show(segment: Segment) -> tuple[str, str | None]:
    """去掉段上的段落号。叶子本身没有 id 时，用切段时记下的祖先 id。"""
    html, anchor = _take_anchor(segment.html)
    return html, anchor or segment.anchor


def _blocks_for_pairs(
    original_html: str, zh_html: str, *, dedupe: bool = False
) -> tuple[list[Segment], list[Segment]] | None:
    """只返回有序原始叶子；兼容 dedupe 参数，但不再删段或改用粗粒度凑数量。"""
    left, right = _entries(original_html), _entries(zh_html)
    return (left, right) if left or right else None


_WORDS = re.compile(r"\w+")
_SRC_ATTR = re.compile(r"""\bsrc=["']([^"']*)["']""", re.I)
_HREF_ATTR = re.compile(r"""\bhref=["']([^"']*)["']""", re.I)


def _material_facts(html: str) -> set[str]:
    """图片地址和链接地址。这两个收掉就找不回来了，重复段也不行。"""
    raw = html or ""
    facts = {f"i:{html_lib.unescape(src)}" for src in _SRC_ATTR.findall(raw)}
    facts.update(f"a:{html_lib.unescape(href)}" for href in _HREF_ATTR.findall(raw))
    return facts


def pair_facts(html: str) -> set[str]:
    """收掉这一对会不会丢原文内容：可见文字、图片地址、链接地址。"""
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html or ""))
    return {f"w:{word}" for word in _WORDS.findall(text)} | _material_facts(html)


def _repeat_candidates(pairs: Sequence[BilingualPair]) -> list[int]:
    """页面上看起来重复的 pair：重复、整句包含、近前缀堆，不管删了会不会丢东西。"""
    en_keys = [_leaf_key(pair.en or "") for pair in pairs]
    zh_keys = [_leaf_key(pair.zh or "") for pair in pairs]
    texts = [visible_text(pair.en or "") for pair in pairs]
    redundant: set[int] = set()
    priors: dict[str, list[tuple[str, str]]] = {}
    for index, pair in enumerate(pairs):
        if _display_long(pair.en or ""):
            bucket = en_keys[index][:48]
            repeated = any(
                same_repeat(prior_en, en_keys[index]) and _same_lineage(prior_zh, zh_keys[index])
                for prior_en, prior_zh in priors.get(bucket, ())
            )
            if repeated:
                redundant.add(index)
                continue
            priors.setdefault(bucket, []).append((en_keys[index], zh_keys[index]))
    for index, short in enumerate(texts):
        if len(short) < 80:
            continue
        for other, long in enumerate(texts):
            if other == index or len(long) <= len(short):
                continue
            if short in long:
                redundant.add(index)
                break
            if (len(long) - len(short)) / len(long) > 0.12:
                continue
            if _shared_prefix_len(short, long) < len(short) * 0.75:
                continue
            if not zh_keys[index] or (zh_keys[other] and _same_lineage(zh_keys[other], zh_keys[index])):
                redundant.add(index)
                break
    return sorted(redundant)


def redundant_pairs(pairs: Sequence[BilingualPair]) -> list[int]:
    """诊断可能重复的位置，不授权删除。

    这份兼容指标仍用词项和资料交集找相似段，不能证明两份译法含义相同。
    页面保留全部出现，只有两侧 HTML 完全一致时才作可展开折叠。
    """
    candidates = _repeat_candidates(pairs)
    if not candidates:
        return []
    en_facts = [pair_facts(pair.en or "") for pair in pairs]
    visible = [en_facts[index] | _material_facts(pairs[index].zh or "") for index in range(len(pairs))]
    en_counts: dict[str, int] = {}
    visible_counts: dict[str, int] = {}
    for index, facts in enumerate(en_facts):
        for fact in facts:
            en_counts[fact] = en_counts.get(fact, 0) + 1
        for fact in visible[index]:
            visible_counts[fact] = visible_counts.get(fact, 0) + 1
    drop: list[int] = []
    for index in sorted(candidates, reverse=True):
        if any(en_counts.get(fact, 0) < 2 for fact in en_facts[index]):
            continue
        if any(visible_counts.get(fact, 0) < 2 for fact in visible[index]):
            continue
        drop.append(index)
        for fact in en_facts[index]:
            en_counts[fact] -= 1
        for fact in visible[index]:
            visible_counts[fact] -= 1
    return sorted(drop)


def display_blocks(
    original_html: str, zh_html: str, *, dedupe: bool = True
) -> tuple[list[Segment], list[Segment]] | None:
    """有序原始叶子；dedupe 参数保留兼容，不再整理条数。

    数量不同时由调用方明确处理，不单侧删除或改用粗粒度来凑数量。
    """
    return _blocks_for_pairs(original_html, zh_html, dedupe=dedupe)


def pairs_from_blocks(kids_o: Sequence[Segment], kids_t: Sequence[Segment]) -> list[BilingualPair]:
    """结构候选；条数不同时不配位置。页面应使用带状态的 article_pairs。"""
    if len(kids_o) != len(kids_t):
        return []
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
    original_html: str, zh_html: str, *, dedupe: bool = False
) -> list[BilingualPair] | None:
    """兼容结构入口，不再靠去重／粗分段修复条数；无法配对时返回 None。

    等长仍不证明语义对应。完整页面用 article_pairs 读取回执并展示可信度。
    """
    from researchlib.translate.provenance import leaf_problem

    blocks = _blocks_for_pairs(original_html, zh_html)
    if blocks is None or len(blocks[0]) != len(blocks[1]):
        return None
    if any(leaf_problem(a.html, b.html) == "numbers" for a, b in zip(*blocks, strict=True)):
        return None
    return pairs_from_blocks(*blocks)

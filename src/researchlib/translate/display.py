"""对照的完整性保护：叶子投影有遗漏时，回到完整区块；段数不齐时不硬配。

细对齐（叶子按位置并排）是默认展示。软去重属于展示层：它只该收掉页面上重复的那几对，
不能把投影检查弄失败、再整页退回「整篇英文 + 整篇中文」。所以顺序是
「不去重的细对 → 投影检查 → 展示层收重复」，收重复时只收删了也不丢原文内容的 pair。

投影有缺口时先看整块对照（标题、表格和列表外壳留着）；整块对不上、缺口又正好等于
切叶子本来就带不走的那点内容时，仍用细对。只有细对本身在丢正文（两边叶子条数不同，
或缺口不是切叶子造成的）才分别展示完整原文和译文。
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from researchlib.translate.pair import (
    BilingualPair,
    bilingual_pairs,
    display_blocks,
    pairs_from_blocks,
    redundant_pairs,
    visible_text,
)
from researchlib.translate.segments import Segment, iter_segments, top_level_blocks

_STRUCTURE = frozenset({"table", "thead", "tbody", "tr", "td", "th", "ul", "ol", "dl", "dt", "dd", "pre", "code"})
_PARA_ID = re.compile(r'\s+id="p-\d+"')
_ROOT = re.compile(r"^<([a-z][a-z0-9]*)\b", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_SRC = re.compile(r'''\bsrc=["']([^"']*)["']''', re.I)


class _Facts(HTMLParser):
    def __init__(self, document: str):
        super().__init__()
        self.structures: set[tuple] = set()
        self.media: set[str] = set()
        self.links: set[str] = set()
        self.tokens: set[str] = set()
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _STRUCTURE:
            # 表格合并单元格、列表起始序号也属于内容，不能只检查有没有文字。
            self.structures.add((tag, attrs.get("start"), "reversed" in attrs,
                                 attrs.get("rowspan"), attrs.get("colspan"), attrs.get("value")))
        if tag == "img" and attrs.get("src"):
            self.media.add(attrs["src"])
        if tag == "a" and attrs.get("href"):
            self.links.add(attrs["href"])

    def handle_data(self, data):
        self.tokens.update(re.findall(r"\w+", data))


def content_gaps(source: str, displayed: str) -> int:
    """独立于可译段的投影检查。重复词可折叠，独有文字、数字、图片和结构必须还在。

    这只是结构完整性检查，不证明译文语义准确，也不能发现所有同词错位。
    """
    before, after = _Facts(source), _Facts(displayed)
    return (len(before.tokens - after.tokens) + len(before.media - after.media) + len(before.links - after.links)
            + len(before.structures - after.structures))


def protected_content_gaps(source: str, translated: str) -> int:
    """译制未处理的图片、代码和纯数字区块，不应在译文里消失。"""
    blocks = top_level_blocks(source)
    protected = [block for block in blocks if not iter_segments(block)]
    # 数字单元格可能嵌在表格里，不是顶层块。
    protected.extend(re.findall(r"<(?:td|th)\b[^>]*>\s*([\d\s.,%+−—/-]+)\s*</(?:td|th)>", source, re.I))
    return content_gaps("".join(protected), translated)


@dataclass(frozen=True)
class PairDisplay:
    pairs: list[BilingualPair]
    notice: str = ""


def _gaps(source_en: str, source_zh: str, pairs: Sequence[BilingualPair]) -> tuple[int, int]:
    en = "".join(pair.en for pair in pairs)
    visible = "".join(pair.en + (pair.zh or "") for pair in pairs)
    return content_gaps(source_en, en), content_gaps(source_zh, visible)


def projection_gaps(original: str, translated: str, pairs: Sequence[BilingualPair]) -> tuple[int, int]:
    """检查最终展示：原文在原文侧完整，译文的共享图片允许只出现在原文侧。"""
    return _gaps(original, translated, pairs)


def leaf_projection_gaps(
    original: str, translated: str, pairs: Sequence[BilingualPair]
) -> tuple[int, int, int]:
    """展示的 pairs 相对细对齐叶子缺了什么：原文侧、译文侧、译文侧的图片和链接。

    细对齐叶子是送去译制的那一份；它没带上的图片链接、纯数字块不算展示层遗漏，
    由 ``protected_content_gaps`` 单独看。软去重是唯一会在这里现形的删减：收掉重复段时
    另一种译法的措辞会少，原文侧和资料链接不能少。
    """
    leaves = display_blocks(original, translated, dedupe=False)
    if leaves is None:
        return (*projection_gaps(original, translated, pairs), 0)
    source_loss = content_gaps("".join(item.html for item in leaves[0]), "".join(pair.en for pair in pairs))
    visible = "".join(pair.en + (pair.zh or "") for pair in pairs)
    translation_loss = content_gaps("".join(item.html for item in leaves[1]), visible)
    return source_loss, translation_loss, _material_gaps("".join(item.html for item in leaves[1]), visible)


def _material_gaps(source: str, displayed: str) -> int:
    """只看图片和链接：另一种译法的措辞可以收，资料链接不能少。"""
    before, after = _Facts(source), _Facts(displayed)
    return len(before.media - after.media) + len(before.links - after.links)


def _duplicate_free(pairs: list[BilingualPair]) -> list[BilingualPair]:
    """展示层收重复：只收掉删了也不丢原文内容的重复 pair。

    ``redundant_pairs`` 与质检共用一套门槛，页面上剩下的都不是能安全收掉的重复。
    """
    drop = set(redundant_pairs(pairs))
    if not drop:
        return pairs
    return [pair for index, pair in enumerate(pairs) if index not in drop]


def _shared_media_removed(en: str, zh: str) -> str:
    shared = _Facts(en).media

    def keep(match):
        src = _SRC.search(match.group())
        return "" if src and html.unescape(src.group(1)) in shared else match.group()

    return _IMG.sub(keep, zh)


def _whole_pair(en: str, zh: str) -> BilingualPair:
    anchor = re.search(r'\bid="(p-\d+)"', zh) or re.search(r'\bid="(p-\d+)"', en)
    en, zh = _PARA_ID.sub("", en), _PARA_ID.sub("", zh)
    zh = _shared_media_removed(en, zh)
    if visible_text(en) == visible_text(zh) and not content_gaps(zh, en):
        zh = ""
    return BilingualPair(en=en, zh=zh or None, anchor=anchor.group(1) if anchor else None)


def _separate(original: str, translated: str, notice: str) -> PairDisplay:
    # 译文块的 p-N 引用仍落在译文；原文不抢这些 id，图片锚点只留一份。
    en = _PARA_ID.sub("", original)
    zh = _shared_media_removed(en, translated)
    return PairDisplay([BilingualPair(en, None), BilingualPair("", zh or None)], notice)


def _checked_display(original: str, translated: str, pairs: list[BilingualPair]) -> PairDisplay:
    # 所有区块处理结束后再核对一次；不能假定中途的保留检查覆盖后续整理。
    if any(projection_gaps(original, translated, pairs)):
        return _separate(original, translated, "无法完整逐段展示，以下分别保留原文和译文。")
    return PairDisplay(pairs)


def _whole_blocks(original: str, translated: str) -> PairDisplay | None:
    """整块对照：每个顶层区块内部还配得上的，仍然逐段并排。区块结构对不上返回 None。"""
    left, right = top_level_blocks(original), top_level_blocks(translated)
    compatible = len(left) == len(right) and all(
        (_ROOT.match(en).group(1) if _ROOT.match(en) else "")
        == (_ROOT.match(zh).group(1) if _ROOT.match(zh) else "")
        and len(iter_segments(en)) == len(iter_segments(zh))
        for en, zh in zip(left, right, strict=True)
    )
    if not compatible:
        return None
    pairs: list[BilingualPair] = []
    for en, zh in zip(left, right, strict=True):
        projected = bilingual_pairs(en, zh) or []
        if any(projection_gaps(en, zh, projected)):
            # 这一块里的细对会丢内容：整块留下，资料链接和图片都在。
            pairs.append(_whole_pair(en, zh))
        else:
            pairs.extend(projected)
    checked = _checked_display(original, translated, pairs)
    if checked.notice:
        return checked
    # 完整区块确认无误之后再收重复：只收原文内容在别处都还在的那几对。
    return PairDisplay(_duplicate_free(checked.pairs))


def _legacy_display(original: str, translated: str) -> PairDisplay:
    """两边叶子条数不同的老办法：先在同一套去重里把条数拉齐，再配位置。"""
    leaves = display_blocks(original, translated)
    if leaves and len(leaves[0]) != len(leaves[1]):
        # 无法知道漏的是中间哪一段。把多余段挂到末尾会把后面的每一句都配错。
        return _separate(
            original, translated,
            "译文段落与原文对不上，以下分别展示完整原文和译文。",
        )
    raw = bilingual_pairs(original, translated) or []
    if not any(projection_gaps(original, translated, raw)):
        return PairDisplay(raw)
    whole = _whole_blocks(original, translated)
    if whole is not None:
        return whole
    return _separate(
        original, translated,
        "这篇的结构无法逐段对应，以下分别展示完整原文和译文。",
    )


def _leaf_extraction_gaps(
    original: str, translated: str, leaves: tuple[list[Segment], list[Segment]]
) -> tuple[int, int]:
    """切叶子这一步本身相对整篇原文丢了什么：标题、表格外壳、图片链接这些不送译的内容。"""
    return (
        content_gaps(original, "".join(item.html for item in leaves[0])),
        content_gaps(translated, "".join(item.html for item in leaves[1])),
    )


def _shown_pairs(
    original: str, translated: str, full: list[BilingualPair], loss: tuple[int, int]
) -> PairDisplay:
    """收重复之后再看一眼原文侧：真丢了原文里的东西就整份细对都留着。"""
    shown = _duplicate_free(full)
    if projection_gaps(original, translated, shown)[0] != loss[0]:
        return PairDisplay(full)
    return PairDisplay(shown)


def article_pairs(original: str, translated: str) -> PairDisplay:
    """普通段沿用细分对照；软去重只在展示层收，不把整页打回两篇长文。

    顺序：完整细对 → 投影检查 → 展示层收重复。软去重放在投影检查之后：收掉重复的
    那几对之后，原文若比细对少了内容，就整份细对都留着，也不再退成两篇长文。
    """
    leaves = display_blocks(original, translated, dedupe=False)
    if leaves is None:
        return PairDisplay([])
    if len(leaves[0]) != len(leaves[1]):
        # 无法知道漏的是中间哪一段。把多余段挂到末尾会把后面的每一句都配错。
        return _legacy_display(original, translated)
    full = pairs_from_blocks(leaves[0], leaves[1])
    loss = projection_gaps(original, translated, full)
    if not any(loss):
        return _shown_pairs(original, translated, full, loss)
    whole = _whole_blocks(original, translated)
    if whole is not None:
        # 整块对得上时保留完整区块：标题、表格和列表外壳比逐句并排更重要。
        return whole
    if loss != _leaf_extraction_gaps(original, translated, leaves):
        # 细对本身就在丢内容，不是切叶子带走的：这时逐段并排会漏正文。
        return _separate(
            original, translated,
            "这篇的结构无法逐段对应，以下分别展示完整原文和译文。",
        )
    return _shown_pairs(original, translated, full, loss)


__all__ = [
    "PairDisplay",
    "article_pairs",
    "content_gaps",
    "leaf_projection_gaps",
    "projection_gaps",
    "protected_content_gaps",
]

"""对照的完整性保护：叶子投影有遗漏时，回到完整区块；段数不齐时不硬配。"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from researchlib.translate.pair import BilingualPair, bilingual_pairs, display_blocks, visible_text
from researchlib.translate.segments import iter_segments, top_level_blocks

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


def projection_gaps(original: str, translated: str, pairs: Sequence[BilingualPair]) -> tuple[int, int]:
    """检查最终展示：原文在原文侧完整，译文的共享图片允许只出现在原文侧。"""
    en = "".join(pair.en for pair in pairs)
    visible = "".join(pair.en + (pair.zh or "") for pair in pairs)
    return content_gaps(original, en), content_gaps(translated, visible)


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


def article_pairs(original: str, translated: str) -> PairDisplay:
    """普通段沿用细分对照；无法无损投影的表格、列表等按完整原区块显示。"""
    leaves = display_blocks(original, translated)
    raw = bilingual_pairs(original, translated) or []
    if leaves and len(leaves[0]) != len(leaves[1]):
        # 无法知道漏的是中间哪一段。把多余段挂到末尾会把后面的每一句都配错。
        return _separate(
            original, translated,
            "译文段落与原文对不上，以下分别展示完整原文和译文。",
        )
    if not any(projection_gaps(original, translated, raw)):
        return PairDisplay(raw)
    left, right = top_level_blocks(original), top_level_blocks(translated)
    compatible = len(left) == len(right) and all(
        (_ROOT.match(en).group(1) if _ROOT.match(en) else "")
        == (_ROOT.match(zh).group(1) if _ROOT.match(zh) else "")
        and len(iter_segments(en)) == len(iter_segments(zh))
        for en, zh in zip(left, right, strict=True)
    )
    if compatible:
        pairs: list[BilingualPair] = []
        for en, zh in zip(left, right, strict=True):
            projected = bilingual_pairs(en, zh) or []
            # 此处已经为保留内容退回原区块，不再按文字二次去重。
            # 两份中文同文但资料链接或图片不同，必须全部留下。
            if any(projection_gaps(en, zh, projected)):
                pairs.append(_whole_pair(en, zh))
            else:
                pairs.extend(projected)
        return _checked_display(original, translated, pairs)
    return _separate(
        original, translated,
        "这篇的结构无法逐段对应，以下分别展示完整原文和译文。",
    )

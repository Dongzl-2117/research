"""逐段对照与保真检查。

新稿先核验 canonical 源文和持久回执；旧稿保留明确的不确定性。无法配对时保留两份全文。
展示单元保留引用、列表和表格结构；完全相同的整对可折叠，文字、资源和出现位置仍在。
没有删除译段来凑数量的路径。图片文字在配对后渲染，不参与模型源段身份。
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from researchlib.translate.pair import (
    BilingualPair,
    display_blocks,
    visible_text,
)
from researchlib.translate.segments import iter_segments, top_level_blocks

_STRUCTURE = frozenset({"table", "thead", "tbody", "tr", "td", "th", "ul", "ol", "dl", "dt", "dd", "pre", "code"})
_PARA_ID = re.compile(r'\s+id="p-\d+"')
_ROOT = re.compile(r"^<([a-z][a-z0-9]*)\b", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_SRC = re.compile(r'''\bsrc=["']([^"']*)["']''', re.I)


class _Identifiers(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.ids = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.ids.extend(value for name, value in attrs if name == "id" and value)


def _unique_ids(pairs: list[BilingualPair]) -> list[BilingualPair]:
    """中英两份和列表外壳可能同用脚注 id；只保留一个真实锚点，href 不改写。"""
    from dataclasses import replace

    seen = {p.anchor for p in pairs if p.anchor}

    class Clean(HTMLParser):
        def __init__(self, value):
            super().__init__(convert_charrefs=False)
            self.out = []
            self.feed(value)

        def start(self, tag, attrs, close):
            kept = []
            for name, value in attrs:
                if name == "id":
                    if not value or value in seen:
                        continue
                    seen.add(value)
                kept.append(f' {name}="{html.escape(value, quote=True)}"' if value is not None else f" {name}")
            self.out.append(f"<{tag}{''.join(kept)}{' /' if close else ''}>")

        def handle_starttag(self, tag, attrs):
            self.start(tag, attrs, False)

        def handle_startendtag(self, tag, attrs):
            self.start(tag, attrs, True)

        def handle_endtag(self, tag):
            self.out.append(f"</{tag}>")

        def handle_data(self, data):
            self.out.append(data)

        def handle_entityref(self, name):
            self.out.append(f"&{name};")

        def handle_charref(self, name):
            self.out.append(f"&#{name};")

    # 脚注优先落在中文；中文没有该锚点时仍能落到原文。
    result = [replace(p, zh="".join(Clean(p.zh).out) if p.zh else None) for p in pairs]
    return [replace(p, en="".join(Clean(p.en).out)) for p in result]


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
    protected = []
    for block in blocks:
        if not iter_segments(block):
            protected.append(block)
        else:
            # 整个无叶子表格已保留时不再追加单元格；混合表格保留完整单元格，
            # 避免把相邻的 10 / 20 拼成额外的 1020。
            protected.extend(re.findall(
                r"<(?:td|th)\b[^>]*>\s*[\d\s.,%+−—/-]+\s*</(?:td|th)>", block, re.I,
            ))
    from collections import Counter

    from researchlib.translate.provenance import Resources

    before, after = Resources(source), Resources(translated)
    return (content_gaps("".join(protected), translated)
            + sum((Counter(before.codes) - Counter(after.codes)).values())
            + sum((Counter(before.media) - Counter(after.media)).values()))


@dataclass(frozen=True)
class PairDisplay:
    pairs: list[BilingualPair]
    notice: str = ""
    correspondence: str = "legacy"
    correspondence_reason: str = ""
    leaf_counts: tuple[int, int] = (0, 0)


def _gaps(source_en: str, source_zh: str, pairs: Sequence[BilingualPair]) -> tuple[int, int]:
    en = "".join(pair.en for pair in pairs)
    visible = "".join(pair.en + (pair.zh or "") for pair in pairs)
    return content_gaps(source_en, en), content_gaps(source_zh, visible)


def projection_gaps(original: str, translated: str, pairs: Sequence[BilingualPair]) -> tuple[int, int]:
    """检查最终展示：原文在原文侧完整，译文的共享图片允许只出现在原文侧。"""
    return _gaps(original, translated, pairs)


def ordered_projection_gaps(
    original: str, translated: str, pairs: Sequence[BilingualPair], *, separate: bool = False,
) -> tuple[int, int]:
    """独立检查实际投影的文字顺序和重数；共享媒体可在同一对原文侧出现。"""
    from collections import Counter

    from researchlib.translate.provenance import Resources

    def plain(value):
        return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", value)))

    en = "".join(p.en for p in pairs)
    zh = "".join((p.zh or "") if separate else (p.zh if p.zh is not None else p.en) for p in pairs)
    visible = en + "".join(p.zh or "" for p in pairs)
    left, right, shown_en, shown = Resources(original), Resources(translated), Resources(en), Resources(visible)
    return (
        int(plain(original) != plain(en))
        + sum((Counter(left.links) - Counter(shown_en.links)).values())
        + sum((Counter(left.media) - Counter(shown_en.media)).values()),
        int(plain(translated) != plain(zh))
        + sum((Counter(right.links) - Counter(shown.links)).values())
        + sum((Counter(right.media) - Counter(shown.media)).values()),
    )


def leaf_projection_gaps(
    original: str, translated: str, pairs: Sequence[BilingualPair]
) -> tuple[int, int, int]:
    """展示的 pairs 相对细对齐叶子缺了什么：原文侧、译文侧、译文侧的图片和链接。

    细对齐叶子是送去译制的那一份；它没带上的图片链接、纯数字块不算展示层遗漏，
    由 ``protected_content_gaps`` 单独看。此函数仅保留兼容统计；对应关系由持久回执核验，实际保真由有序投影另查。
    """
    leaves = display_blocks(original, translated, dedupe=False)
    if leaves is None:
        return (*projection_gaps(original, translated, pairs), 0)
    source_loss = content_gaps("".join(item.html for item in leaves[0]), "".join(pair.en for pair in pairs))
    visible = "".join(pair.en + (pair.zh or "") for pair in pairs)
    translation_loss = content_gaps("".join(item.html for item in leaves[1]), visible)
    return source_loss, translation_loss, _material_gaps("".join(item.html for item in leaves[1]), visible)


def _material_gaps(source: str, displayed: str) -> int:
    """兼容资源统计；不授权删除译文文字。"""
    before, after = _Facts(source), _Facts(displayed)
    return len(before.media - after.media) + len(before.links - after.links)


def _shared_media_removed(en: str, zh: str) -> str:
    from researchlib.translate.provenance import Resources

    shared = [key for key in Resources(en).media if key[0] == "img"]
    position = 0
    sharing = True

    def keep(match):
        nonlocal position, sharing
        facts = Resources(match.group()).media
        key = facts[0] if facts else None
        if sharing and position < len(shared) and key == shared[position]:
            position += 1
            return ""
        sharing = False
        return match.group()

    return _IMG.sub(keep, zh)


def _whole_pair(en: str, zh: str) -> BilingualPair:
    anchor = re.search(r'\bid="(p-\d+)"', zh) or re.search(r'\bid="(p-\d+)"', en)
    en, zh = _PARA_ID.sub("", en), _PARA_ID.sub("", zh)
    full_zh = zh
    zh = _shared_media_removed(en, zh)
    if visible_text(en) == visible_text(zh) and not any(
        ordered_projection_gaps(en, full_zh, [BilingualPair(en, None)]),
    ):
        zh = ""
    return BilingualPair(en=en, zh=zh or None, anchor=anchor.group(1) if anchor else None)


def _separate(original: str, translated: str, notice: str) -> PairDisplay:
    # 译文块的 p-N 引用仍落在译文；原文不抢这些 id，图片锚点只留一份。
    en = _PARA_ID.sub("", original)
    zh = _shared_media_removed(en, translated)
    return PairDisplay([BilingualPair(en, None), BilingualPair("", zh or None)], notice)


def article_pairs(
    original: str, translated: str, *, provenance: dict | None = None,
    canonical_source: str | None = None, canonical_target: str | None = None,
    ocr_texts: dict[str, str] | None = None, host: str | None = None,
) -> PairDisplay:
    """逐段身份优先；旧稿只作明确标记的结构对照，不删段凑数量。

    映射由未装饰的 canonical 输入核验；图片文字在配对后加入，避免它参与翻译分段。
    """
    from dataclasses import replace

    from researchlib.translate.layout import paired_units
    from researchlib.translate.provenance import (
        AlignmentError,
        MappingState,
        anchored_legacy_reflow,
        materialize,
        numeric_anchors,
        verify,
    )

    state = verify(canonical_source if canonical_source is not None else original,
                   canonical_target if canonical_target is not None else translated, provenance)
    counts = (len(iter_segments(original)), len(iter_segments(translated)))
    if state.status == "incompatible":
        result = _separate(original, translated, "译文来源或处理版本已变化，以下保留两份全文，需核对对应关系。")
    else:
        try:
            en = materialize(original)
            zh = materialize(translated)
            left, right = iter_segments(en), iter_segments(zh)
            if len(left) != len(right) and state.status == "legacy":
                reflow = anchored_legacy_reflow(en, zh)
                if reflow:
                    en, zh = reflow
                    left, right = iter_segments(en), iter_segments(zh)
                    state = MappingState("legacy-anchors", "unique-ordered-anchors")
            counts = (len(left), len(right))
            if len(left) != len(right):
                result = _separate(original, translated, "译文段落与原文对不上，以下分别展示完整原文和译文。")
            elif any(numeric_anchors(a.html) != numeric_anchors(b.html) for a, b in zip(left, right, strict=True)):
                result = _separate(
                    original, translated, "段落数字锚点不一致，无法确认对应关系，以下保留完整原文和译文。",
                )
            else:
                fragments = paired_units(en, zh)
                if fragments:
                    pairs = []
                    anchors = set()
                    exact_pairs = {}
                    for a, b in fragments:
                        key = (_PARA_ID.sub("", a), _PARA_ID.sub("", b))
                        pair = _whole_pair(a, b)
                        if key in exact_pairs:
                            pair = replace(pair, repeat_of=exact_pairs[key])
                        else:
                            exact_pairs[key] = len(pairs)
                        if pair.anchor in anchors:
                            pair = replace(pair, anchor=None)
                        if pair.anchor:
                            anchors.add(pair.anchor)
                        aliases = []
                        for alias in re.findall(r'\bid="(p-\d+)"', b + a):
                            if alias not in anchors:
                                aliases.append(f'<span id="{alias}" class="para-anchor"></span>')
                                anchors.add(alias)
                        remaining = set(_Identifiers(pair.en + (pair.zh or "")).ids)
                        for alias in _Identifiers(b + a).ids:
                            if re.fullmatch(r"p-\d+", alias) or alias in remaining:
                                continue
                            aliases.append(f'<span id="{html.escape(alias, quote=True)}" class="para-anchor"></span>')
                            remaining.add(alias)
                        if aliases:
                            pair = replace(pair, en="".join(aliases) + pair.en)
                        pairs.append(pair)
                    result = PairDisplay(pairs)
                else:
                    result = _separate(original, translated, "结构无法逐段对应，以下保留完整原文和译文。")
        except AlignmentError:
            result = _separate(original, translated, "分段无法保真，以下保留完整原文和译文。")
    if not result.notice and any(ordered_projection_gaps(original, translated, result.pairs)):
        result = _separate(original, translated, "无法完整逐段展示，以下分别保留原文和译文。")
    if result.notice:
        counts = (len(iter_segments(original)), len(iter_segments(translated)))
    result = replace(result, correspondence=("unmapped" if result.notice else state.status),
                     correspondence_reason=state.reason, leaf_counts=counts)
    result = replace(result, pairs=_unique_ids(result.pairs))
    if ocr_texts:
        from researchlib.render import HOST, inline_image_ocr

        result = replace(result, pairs=[replace(p,
            en=inline_image_ocr(p.en, ocr_texts, host=host or HOST),
            zh=inline_image_ocr(p.zh, ocr_texts, host=host or HOST) if p.zh else None,
        ) for p in result.pairs])
    return result


__all__ = [
    "PairDisplay",
    "article_pairs",
    "content_gaps",
    "leaf_projection_gaps",
    "projection_gaps",
    "protected_content_gaps",
]

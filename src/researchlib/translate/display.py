"""逐段对照与保真检查。

新稿先核验 canonical 源文和持久回执；旧稿保留明确的不确定性。无法配对时保留两份全文。
展示单元保留引用、列表和表格结构。有序投影通过之后，再隐藏长英文的重复、整句包含和近前缀；
留下的那一对必须仍有这些文字、数字、链接和结构，锚点改挂过去。中文各自带独有说法的不隐藏。
这一步不删入库的段，也不把整页退回两列。图片文字在配对后渲染，不参与模型源段身份。
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from researchlib.translate.pair import (
    BilingualPair,
    _leaf_key,
    _same_lineage,
    display_blocks,
    redundant_pairs,
    visible_text,
)
from researchlib.translate.segments import iter_segments, repeat_key, same_repeat, top_level_blocks

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


_LINK = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def _plain_words(value: str) -> set[str]:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return set(re.findall(r"\w+", text))


def _hrefs(value: str) -> set[str]:
    return {html.unescape(item) for item in _HREF.findall(value or "")}


def _outside_tag(value: str, index: int) -> bool:
    return value.rfind("<", 0, index) < value.rfind(">", 0, index)


def _inside_open_anchor(value: str, index: int) -> bool:
    return value.lower().rfind("<a", 0, index) > value.lower().rfind("</a", 0, index)


def _graft_links(donor: str, host: str, *, first_of_many: bool) -> str | None:
    """把 donor 里 host 还没有的链接套到 host 的同一段可见文字上。套不上就返回 None。"""
    donor_imgs = {html.unescape(src) for src in _IMG_SRC.findall(donor or "")}
    host_imgs = {html.unescape(src) for src in _IMG_SRC.findall(host or "")}
    if not donor_imgs <= host_imgs:
        return None
    updated = host or ""
    for attrs, inner in _LINK.findall(donor or ""):
        href_match = _HREF.search(attrs)
        if not href_match:
            return None
        href = html.unescape(href_match.group(1))
        if href in _hrefs(updated):
            continue
        anchor = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", inner))).strip()
        parts = [re.escape(part) for part in anchor.split() if part]
        if len(anchor) < 4 or not parts:
            return None
        matches = [
            match for match in re.finditer(r"\s+".join(parts), updated, re.I)
            if _outside_tag(updated, match.start()) and not _inside_open_anchor(updated, match.start())
        ]
        if len(matches) != 1:
            if not first_of_many or not matches:
                return None
            matches = matches[:1]
        match = matches[0]
        wrapped = f'<a href="{html.escape(href, quote=True)}">{match.group(0)}</a>'
        updated = updated[: match.start()] + wrapped + updated[match.end() :]
    return updated


def _absorb_contained(
    pairs: list[BilingualPair], blocked: set[int],
) -> tuple[list[BilingualPair], set[int]]:
    """短的长英文整段包含在更长的一段里时，把独有链接挪到长段上，短的不再显示。"""
    from dataclasses import replace

    keys = [repeat_key(pair.en or "") for pair in pairs]
    texts = [visible_text(pair.en or "") for pair in pairs]
    updated = list(pairs)
    drop: set[int] = set()
    for index in sorted(range(len(pairs)), key=lambda item: len(keys[item])):
        if index in drop or index in blocked or len(texts[index]) < 80 or not keys[index]:
            continue
        hosts = []
        for other, key in enumerate(keys):
            if other == index or other in blocked or other in drop or len(key) <= len(keys[index]):
                continue
            contained = texts[index] in texts[other] and len(texts[other]) > len(texts[index])
            if not (key.startswith(keys[index]) or contained):
                continue
            if not _plain_words(updated[index].en) <= _plain_words(updated[other].en):
                continue
            donor_zh = updated[index].zh or ""
            host_zh = updated[other].zh or ""
            if not _hrefs(donor_zh) <= _hrefs((updated[other].en or "") + host_zh):
                continue
            same_zh = _plain_words(donor_zh) <= _plain_words(host_zh)
            if not same_zh and not _same_lineage(_leaf_key(host_zh), _leaf_key(donor_zh)):
                continue
            hosts.append(other)
        if not hosts:
            continue
        host = max(hosts, key=lambda item: len(keys[item]))
        grafted = _graft_links(updated[index].en or "", updated[host].en or "", first_of_many=True)
        if grafted is None:
            continue
        zh = updated[host].zh or ""
        extra = updated[index].zh or ""
        if extra and not _plain_words(extra) <= _plain_words(zh):
            zh += extra
        updated[host] = replace(updated[host], en=grafted, zh=zh or None)
        drop.add(index)
    return updated, drop


def _element_ids(pair: BilingualPair) -> list[str]:
    found = [pair.anchor] if pair.anchor else []
    found.extend(re.findall(r'\bid="([^"]+)"', (pair.en or "") + (pair.zh or "")))
    return [item for item in found if item]


def _anchor_host(
    index: int, pairs: Sequence[BilingualPair], drop: set[int], keys: Sequence[str],
    buckets: dict[str, list[int]],
) -> int | None:
    """重复段的锚点和多出来的译文挂回仍显示的那一对，不挂到旁边无关的段落。"""
    key = keys[index]
    best = None
    best_len = -1
    for other in buckets.get(key[:48], ()):
        other_key = keys[other]
        related = other_key == key or other_key.startswith(key) or key.startswith(other_key)
        if not related and key and other_key:
            related = same_repeat(key, other_key) or same_repeat(other_key, key)
        if related and len(other_key) >= best_len:
            best, best_len = other, len(other_key)
    if best is not None:
        return best
    kept = [other for other in range(len(pairs)) if other not in drop]
    previous = [other for other in kept if other < index]
    if previous:
        return previous[-1]
    return kept[0] if kept else None


def _hide_repeated_pairs(pairs: list[BilingualPair]) -> list[BilingualPair]:
    """收掉安全的长英文重复。原文侧少了词、链接、图片或结构时，整份都留着。"""
    from dataclasses import replace

    if len(pairs) < 2:
        return pairs
    drop = set(redundant_pairs(pairs))
    updated, absorbed = _absorb_contained(pairs, drop)
    drop |= absorbed
    if not drop or len(drop) >= len(updated):
        return pairs
    keys = [repeat_key(pair.en or "") for pair in updated]
    buckets: dict[str, list[int]] = {}
    for index, key in enumerate(keys):
        if index not in drop:
            buckets.setdefault(key[:48], []).append(index)
    for index in sorted(drop):
        extra = updated[index].zh or ""
        if not extra:
            continue
        host = _anchor_host(index, updated, drop, keys, buckets)
        if host is None:
            continue
        host_zh = updated[host].zh or ""
        if _plain_words(extra) <= _plain_words(host_zh):
            continue
        updated[host] = replace(updated[host], zh=host_zh + extra)
    kept = [index for index in range(len(updated)) if index not in drop]
    shown_en = "".join(updated[index].en for index in kept)
    if content_gaps("".join(pair.en for pair in pairs), shown_en):
        return pairs
    extras: dict[int, list[str]] = {index: [] for index in kept}
    for index in drop:
        host = _anchor_host(index, updated, drop, keys, buckets)
        if host is None:
            continue
        extras[host].extend(_element_ids(updated[index]))
    new_index = {old: new for new, old in enumerate(kept)}
    result = []
    for old in kept:
        pair = updated[old]
        seen = set(_element_ids(pair))
        spans = []
        for ident in extras[old]:
            if ident not in seen:
                spans.append(f'<span id="{html.escape(ident, quote=True)}" class="para-anchor"></span>')
                seen.add(ident)
        target = new_index.get(pair.repeat_of) if pair.repeat_of is not None else None
        if spans or target != pair.repeat_of:
            pair = replace(pair, en=("".join(spans) + pair.en) if spans else pair.en, repeat_of=target)
        result.append(pair)
    return result


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
        positional_numbers_conflict,
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
            elif positional_numbers_conflict([a.html for a in left], [b.html for b in right]):
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
    # 投影先看完整对照。通过之后才隐藏重复；隐藏不得少掉原文里的词、链接或结构，否则整份留着。
    if not result.notice:
        result = replace(result, pairs=_hide_repeated_pairs(result.pairs))
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

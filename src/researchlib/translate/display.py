"""逐段对照与保真检查。

新稿先核验 canonical 源文和持久回执；旧稿保留明确的不确定性。无法配对时保留两份全文。
展示单元保留引用、列表和表格结构。有序投影通过之后，先把同一源段落里按句切开的叶子并回去，
再隐藏长英文的重复、整句包含和近前缀；留下的那一对必须仍有这些文字、数字、链接和结构，锚点改挂过去。
英文连空白都一样、中文却各有独有说法的不隐藏。只差空白的长英文收成一对，多出来的中文句子挂回去。
英文里的链接补到中文仍写着同一锚文本、但丢掉标签的位置，不另写括号域名。
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


def _numeric_table_cells(document: str) -> list[tuple]:
    """数字单元格连同所在行列按序核对；集合会漏掉重复、换位和百分号。"""
    class Tables(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.tables = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'table':
                table = {'row': -1, 'col': -1, 'cell': None, 'facts': []}
                self.tables.append(table)
                self.stack.append(table)
            elif self.stack:
                table = self.stack[-1]
                if tag == 'tr':
                    table['row'] += 1
                    table['col'] = -1
                elif tag in {'td', 'th'}:
                    table['col'] += 1
                    table['cell'] = (table['row'], table['col'], tag,
                                     attrs.get('rowspan'), attrs.get('colspan'), [])

        def handle_data(self, data):
            if self.stack and self.stack[-1]['cell'] is not None:
                self.stack[-1]['cell'][-1].append(data)

        def handle_endtag(self, tag):
            if not self.stack:
                return
            table = self.stack[-1]
            if tag in {'td', 'th'} and table['cell'] is not None:
                *position, fragments = table['cell']
                text = re.sub(r'\s+', '', ''.join(fragments))
                if re.search(r'\d', text) and not any(char.isalpha() for char in text):
                    table['facts'].append((*position, text))
                table['cell'] = None
            elif tag == 'table':
                self.stack.pop()

    parser = Tables()
    parser.feed(document)
    return [tuple(table['facts']) for table in parser.tables]


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
    numeric_tables = int(_numeric_table_cells(source) != _numeric_table_cells(translated))
    return (content_gaps("".join(protected), translated) + numeric_tables
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
    anchor = next((value for value in _Identifiers(zh + en).ids if re.fullmatch(r'p-\d+', value)), None)
    full_zh = zh
    zh = _shared_media_removed(en, zh)
    if visible_text(en) == visible_text(zh) and not any(
        ordered_projection_gaps(en, full_zh, [BilingualPair(en, None)]),
    ):
        zh = ""
    return BilingualPair(en=en, zh=zh or None, anchor=anchor)


def _paragraph_key(value: str) -> str:
    """比较时忽略根块的段落编号；正文、代码和行内命名锚点里的同样字样保留。"""
    class Key(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.out = []
            self.first = True

        def start(self, tag, attrs, close=False):
            if self.first:
                attrs = [(key, val) for key, val in attrs
                         if not (key == 'id' and val and re.fullmatch(r'p-\d+', val))]
                self.first = False
                rendered = ''.join(f' {key}="{html.escape(val, quote=True)}"' if val is not None else f' {key}'
                                   for key, val in attrs)
                self.out.append(f'<{tag}{rendered}{" /" if close else ""}>')
            else:
                self.out.append(self.get_starttag_text())

        def handle_starttag(self, tag, attrs):
            self.start(tag, attrs)

        def handle_startendtag(self, tag, attrs):
            self.start(tag, attrs, True)

        def handle_endtag(self, tag):
            self.out.append(f'</{tag}>')

        def handle_data(self, data):
            self.out.append(data)

        def handle_entityref(self, name):
            self.out.append(f'&{name};')

        def handle_charref(self, name):
            self.out.append(f'&#{name};')

    parser = Key()
    parser.feed(value)
    return ''.join(parser.out)


def _separate(original: str, translated: str, notice: str) -> PairDisplay:
    # 保留真实属性和文字；最终由 HTMLParser 只在实际标签上处理重复 ID。
    en = original
    zh = _shared_media_removed(en, translated)
    return PairDisplay([BilingualPair(en, None), BilingualPair("", zh or None)], notice)


_LINK = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)


def _plain_words(value: str) -> set[str]:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return set(re.findall(r"\w+", text))


_ZH_SENTENCE = re.compile(r"(?<=[。！？])")


def _novel_zh(extra: str, host: str) -> str:
    """host 里还没有的中文句子。整段只是另一种说法时，不要把已经有的句子再贴一遍。"""
    if not extra or _plain_words(extra) <= _plain_words(host):
        return ""
    parts = [part for part in _ZH_SENTENCE.split(extra) if _plain_words(part)]
    if len(parts) <= 1:
        return extra
    host_words = _plain_words(host)
    novel = [part for part in parts if not _plain_words(part) <= host_words]
    if not novel:
        return ""
    if len(novel) == len(parts):
        return extra
    body = "".join(novel).strip()
    if re.search(r"<(?:p|li|blockquote|div|ul|ol|h[1-6])\b", body, re.I):
        return body
    cleaned = re.sub(r"</(?:p|li|ul|ol|blockquote|div|h[1-6])>", "", body, flags=re.I).strip()
    return f"<p>{cleaned}</p>" if cleaned else ""


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
            extra = _drop_duplicate_cites(extra, zh)
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
        dropped_en = updated[index].en or ""
        host_en = updated[host].en or ""
        # 锚点可以挂到相邻段，译文只挂回英文确实是同一段的那一对，避免标题吃进下一段。
        same_passage = bool(visible_text(dropped_en)) and (
            visible_text(dropped_en) in visible_text(host_en)
            or visible_text(host_en) in visible_text(dropped_en)
            or _same_lineage(_leaf_key(host_en), _leaf_key(dropped_en))
        )
        if not same_passage:
            continue
        extra = _novel_zh(_drop_duplicate_cites(extra, host_zh), host_zh)
        if not extra:
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


_SRC_PARA = re.compile(r'\bdata-src-para="(\d+)"')
_CITE = re.compile(
    r"[（(]\s*(?:<a\b[^>]*>\s*)?([A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*(?:</a>\s*)?[）)]",
    re.I,
)
_HOST = re.compile(r"https?://(?:www\.)?([^/?#]+)", re.I)


def _src_para(value: str) -> str | None:
    match = _SRC_PARA.search(value or "")
    return match.group(1) if match else None


def _strip_src_para(value: str) -> str:
    return re.sub(r'\s+data-src-para="\d+"', "", value or "")


def _unwrap_element(value: str) -> tuple[str, str, str] | None:
    match = re.match(r"^(<([a-z0-9]+)\b[^>]*>)(.*)</\2>\s*$", (value or "").strip(), re.I | re.S)
    if not match:
        return None
    return match.group(1), match.group(2).lower(), match.group(3).strip()


def _join_html(parts: list[str]) -> str:
    """把同一源段落拆开的叶子拼回去。外壳只留一份，句子之间补一个空格。"""
    pieces = [part.strip() for part in parts if part and part.strip()]
    if not pieces:
        return ""
    if len(pieces) == 1:
        return _strip_src_para(pieces[0])
    opened = [_unwrap_element(part) for part in pieces]
    if any(item is None for item in opened):
        return _strip_src_para(" ".join(pieces))
    tag = opened[0][1]
    if any(item[1] != tag for item in opened):
        return _strip_src_para(" ".join(pieces))
    inners = [item[2] for item in opened]
    if all(_unwrap_element(inner) for inner in inners):
        inner = _join_html(inners)
    else:
        inner = " ".join(inners)
    opening = _strip_src_para(opened[0][0])
    return f"{opening}{inner}</{tag}>"


def _shown_plain(pairs: Sequence[BilingualPair], side: str) -> str:
    parts = []
    for pair in pairs:
        value = pair.zh if side == "zh" else pair.en
        parts.append(value or "")
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", "".join(parts))))


def _shown_resources(pairs: Sequence[BilingualPair], side: str) -> tuple:
    from researchlib.translate.provenance import Resources

    blob = "".join((pair.zh if side == "zh" else pair.en) or "" for pair in pairs)
    facts = Resources(blob)
    return (tuple(facts.links), tuple(facts.media), tuple(facts.codes))


def _coalesce_preserves(before: Sequence[BilingualPair], after: Sequence[BilingualPair]) -> bool:
    """并段前后，页面上真正画出的中英文、链接和图片要一样。

    有的句子中文和英文相同，对照不单列中文（zh 是 None）。整页投影会把这句英文算进译文侧。
    并进仍有中文的那一段之后，译文侧不再整段退回英文，旧的整页投影会误判丢字，从而放弃所有并段。
    这里只比实际画出来的文字。
    """
    if _shown_plain(before, "en") != _shown_plain(after, "en"):
        return False
    if _shown_plain(before, "zh") != _shown_plain(after, "zh"):
        return False
    return _shown_resources(before, "en") == _shown_resources(after, "en") and (
        _shown_resources(before, "zh") == _shown_resources(after, "zh")
    )


def _coalesce_source_paragraphs(
    pairs: list[BilingualPair], groups: Sequence[str | None] | None = None,
) -> list[BilingualPair]:
    """按句切开的同一源段落并回一对。``<br>`` 粘出来的长文没有这个标记，仍按句留。

    ``groups`` 来自原文 materialize 后的叶子，和对照叶同一顺序。配对 HTML 上的
    ``data-src-para`` 丢了也按这个并。
    """
    from dataclasses import replace

    def gid_at(index: int, pair: BilingualPair) -> str | None:
        if groups is not None and index < len(groups) and groups[index]:
            return groups[index]
        return _src_para(pair.en or "")

    grouped: list[tuple[str | None, list[BilingualPair]]] = []
    for index, pair in enumerate(pairs):
        gid = gid_at(index, pair)
        if gid and grouped and grouped[-1][0] == gid:
            grouped[-1][1].append(pair)
        else:
            grouped.append((gid, [pair]))
    result: list[BilingualPair] = []
    for _gid, group in grouped:
        if len(group) == 1:
            pair = group[0]
            result.append(replace(
                pair,
                en=_strip_src_para(pair.en),
                zh=_strip_src_para(pair.zh) if pair.zh else pair.zh,
            ))
            continue
        anchors: list[str] = []
        for pair in group:
            if pair.anchor and pair.anchor not in anchors:
                anchors.append(pair.anchor)
        en = _join_html([pair.en for pair in group])
        zh_parts = [pair.zh or "" for pair in group]
        zh = _join_html(zh_parts) if any(zh_parts) else None
        extra = [
            anchor for anchor in anchors[1:]
            if f'id="{anchor}"' not in en and f"id='{anchor}'" not in en
        ]
        if extra:
            spans = "".join(
                f'<span id="{html.escape(anchor, quote=True)}" class="para-anchor"></span>'
                for anchor in extra
            )
            en = spans + en
        result.append(BilingualPair(en=en, zh=zh or None, anchor=anchors[0] if anchors else None))
    return result


def _http_host(href: str) -> str:
    match = _HOST.match(html.unescape(href or "").strip())
    return match.group(1).lower() if match else ""


def _href_count(value: str, href: str) -> int:
    return sum(1 for item in _HREF.findall(value or "") if html.unescape(item) == href)


def _wrap_text(updated: str, href: str, anchor: str) -> str | None:
    parts = [re.escape(part) for part in anchor.split() if part]
    if len(anchor) < 4 or not parts:
        return None
    matches = [
        match for match in re.finditer(r"\s+".join(parts), updated, re.I)
        if _outside_tag(updated, match.start()) and not _inside_open_anchor(updated, match.start())
    ]
    if not matches:
        return None
    match = matches[0]
    wrapped = f'<a href="{html.escape(href, quote=True)}">{match.group(0)}</a>'
    return updated[: match.start()] + wrapped + updated[match.end() :]


def _wrap_naked_host(updated: str, href: str, anchor: str) -> str | None:
    names: list[str] = []
    host = _http_host(href)
    if host:
        names.append(host)
    if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", anchor or "") and anchor.lower() not in names:
        names.append(anchor)
    for name in names:
        pattern = re.compile(rf"[（(]\s*({re.escape(name)})\s*[）)]", re.I)
        for match in pattern.finditer(updated):
            if not _outside_tag(updated, match.start(1)) or _inside_open_anchor(updated, match.start(1)):
                continue
            wrapped = f'<a href="{html.escape(href, quote=True)}">{match.group(1)}</a>'
            return updated[: match.start(1)] + wrapped + updated[match.end(1) :]
    return None


def _mirror_links(donor: str, host: str) -> str:
    """donor 里有、host 里还没有的链接，套到 host 已有的锚文本或括号域名上。不新增文字。"""
    pending: list[tuple[str, str]] = []
    for attrs, inner in _LINK.findall(donor or ""):
        href_match = _HREF.search(attrs)
        if not href_match:
            continue
        href = html.unescape(href_match.group(1))
        anchor = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", inner))).strip()
        if href and anchor:
            pending.append((href, anchor))
    updated = host or ""
    for href, anchor in pending:
        wanted = sum(1 for item, _anchor in pending if item == href)
        if _href_count(updated, href) >= wanted:
            continue
        placed = _wrap_text(updated, href, anchor) or _wrap_naked_host(updated, href, anchor)
        if placed is not None:
            updated = placed
    return updated


def _restore_pair_links(pairs: list[BilingualPair]) -> list[BilingualPair]:
    from dataclasses import replace

    restored: list[BilingualPair] = []
    for pair in pairs:
        if not pair.zh:
            restored.append(pair)
            continue
        zh = _mirror_links(pair.en or "", pair.zh)
        en = _mirror_links(zh, pair.en or "")
        if en == (pair.en or "") and zh == pair.zh:
            restored.append(pair)
        else:
            restored.append(replace(pair, en=en, zh=zh))
    return restored


def _cite_hosts(value: str) -> set[str]:
    return {match.group(1).casefold() for match in _CITE.finditer(value or "")}


def _drop_duplicate_cites(extra: str, host: str) -> str:
    """挂译文时，host 已经有的括号域名不再贴第二次。不同 href 的仍留下。"""
    present = _cite_hosts(host)
    host_hrefs = _hrefs(host)

    def repl(match: re.Match[str]) -> str:
        hrefs = _hrefs(match.group(0))
        if hrefs and hrefs <= host_hrefs:
            return ""
        if not hrefs and match.group(1).casefold() in present:
            return ""
        return match.group(0)

    return _CITE.sub(repl, extra or "")


def article_pairs(
    original: str, translated: str, *, provenance: dict | None = None,
    canonical_source: str | None = None, canonical_target: str | None = None,
    ocr_texts: dict[str, str] | None = None, host: str | None = None,
) -> PairDisplay:
    """逐段身份优先；旧稿只作明确标记的结构对照，不删段凑数量。

    映射由未装饰的 canonical 输入核验。识图文字不进对照页（``inline_image_ocr`` 保留原图），
    避免把转写铺进英文或中文栏；摘要、问库和检索另走 present。
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
                        key = (_paragraph_key(a), _paragraph_key(b))
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
                        remaining = set(_Identifiers(pair.en + (pair.zh or "")).ids)
                        for alias in _Identifiers(b + a).ids:
                            if re.fullmatch(r'p-\d+', alias) and alias not in anchors and alias not in remaining:
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
    # 投影先看完整对照。按原文叶子的分段标记把同一段并回去；并段前后页面上的字和链接不变才采用。
    # 旧粗段按唯一锚点展开成句之后不再并回去，否则整段又合成一对。
    # 然后才隐藏重复；隐藏不得少掉原文里的词、链接或结构，否则整份留着。
    if not result.notice:
        if state.status != "legacy-anchors":
            groups = [_src_para(item.html) for item in left] if len(left) == len(result.pairs) else None
            coalesced = _coalesce_source_paragraphs(result.pairs, groups)
            # 整页投影会把「中文和英文相同、所以不单列」算成译文侧丢字，从而放弃所有并段。
            # 只要求并段前后页面上的文字、链接和图片不变。
            if _coalesce_preserves(result.pairs, coalesced):
                result = replace(result, pairs=coalesced)
        result = replace(result, pairs=_hide_repeated_pairs(result.pairs))
        result = replace(result, pairs=_restore_pair_links(result.pairs))
    if result.notice:
        counts = (len(iter_segments(original)), len(iter_segments(translated)))
    result = replace(result, correspondence=("unmapped" if result.notice else state.status),
                     correspondence_reason=state.reason, leaf_counts=counts)
    result = replace(result, pairs=_unique_ids(result.pairs))
    if ocr_texts:
        from researchlib.render import HOST, inline_image_ocr

        # 阅读页不把识图换成对话。这里仍走同一函数，确认图留在配对之后的 HTML 里。
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

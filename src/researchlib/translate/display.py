"""逐段对照与保真检查。

新稿先核验 canonical 源文和持久回执；旧稿保留明确的不确定性。无法配对时保留两份全文。
展示单元保留引用、列表和表格结构。有序投影通过之后，先把同一源段落里按句切开的叶子并回去，
导出写两遍的引文、列表项、脚注按原文页同一条规则收成一份（留带格式的那份），
再隐藏长英文的重复、整句包含和近前缀；留下的那一对必须仍有这些文字、数字、链接和结构，锚点改挂过去。
英文连空白都一样、中文却各有独有说法的不隐藏。只差空白的长英文收成一对，收掉那份的中文只挂页面上还没说过的句子：
同一句换了说法、或已由同一段英文的另几对显示的不再挂，带着别处没有的数字、英文词的照样挂。
英文里的链接补到中文仍写着同一锚文本、但丢掉标签的位置，不另写括号域名。
这一步不删入库的段，也不把整页退回两列。图片文字在配对后渲染，不参与模型源段身份。
"""

from __future__ import annotations

import difflib
import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from researchlib.htmlentities import entity_ref
from researchlib.nested_copies import CopyRun, copy_runs, plain_ends
from researchlib.translate.pair import (
    BilingualPair,
    _leaf_key,
    _same_lineage,
    display_blocks,
    redundant_pairs,
    visible_text,
)
from researchlib.translate.segments import (
    _El,
    _FragmentParser,
    _outer,
    _serialize,
    _Tx,
    iter_segments,
    repeat_key,
    same_repeat,
    top_level_blocks,
)

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
            self.out.append(entity_ref(name))

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


_ZH_SENTENCE = re.compile(r"(?<=[。！？])")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_LATIN_WORD = re.compile(r"[a-z][a-z0-9]+")
_TAG = re.compile(r"<(/?)([a-z][a-z0-9]*)\b[^>]*>", re.I)
_VOID = frozenset({"br", "img", "wbr"})
_INLINE = frozenset({
    "a", "abbr", "b", "cite", "code", "del", "em", "i", "ins", "kbd", "mark", "q", "s", "small", "span",
    "strong", "sub", "sup", "u",
})
# 两份译文译的是同一段英文。换了说法的同一句，去掉标签和标点以后大半的字仍按顺序对得上：
# 11-10 拿线上 4 篇里 23 处写两遍的真实译文试，按这条线一句都不再挂。反过来，28 篇对照页里
# 没有重复的 3265 句拿同一对里别的句子（一句或相邻两句）去比，到七成的只有 4 句，其中 2 句本来就是重复。
# 线再往下放，留下的那份正好漏译一句时，容易把同一段里只差几个字的另一句话当成说过。
_SAID = 0.7
# 短句和别处只差一两个字的多（「现在不一样了」），短过这个要整句出现过才算说过。
_SAID_MIN = 10


def _sentence_core(value: str) -> str:
    return repeat_key(value)


def _coverage(core: str, other: str) -> float:
    """core 里有多少字按顺序在 other 里出现，只算连着两个字以上的。"""
    if not core or not other:
        return 0.0
    if core in other:
        return 1.0
    matcher = difflib.SequenceMatcher(None, core, other, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks() if block.size >= 2) / len(core)


class _Said:
    """已经显示的中文。判断挂回去的一句是不是只换了说法：数字、英文词都出现过，字也大半对得上。"""

    def __init__(self, shown: str):
        visible = html.unescape(re.sub(r"<[^>]+>", " ", shown or "")).casefold()
        self.core = _sentence_core(shown)
        self.numbers = set(_NUMBER.findall(visible))
        self.words = set(_LATIN_WORD.findall(visible))
        cores = [_sentence_core(part) for part in _ZH_SENTENCE.split(shown or "")]
        cores = [core for core in cores if core]
        # 一句可能被另一份拆成两句译，所以也拿相邻两句连起来比。
        self.windows = cores + [left + right for left, right in zip(cores, cores[1:], strict=False)]
        self.bigrams = [{window[at:at + 2] for at in range(len(window) - 1)} for window in self.windows]

    def __call__(self, sentence: str) -> bool:
        core = _sentence_core(sentence)
        if not core:
            return True
        if core in self.core:
            return True
        visible = html.unescape(re.sub(r"<[^>]+>", " ", sentence)).casefold()
        if set(_NUMBER.findall(visible)) - self.numbers:
            return False
        for word in _LATIN_WORD.findall(visible):
            if word not in self.words and (len(word) < 4 or word not in self.core):
                return False
        if len(core) < _SAID_MIN:
            return False
        # 对上的字最多是共有的两字组的两倍，先用它筛掉不可能到线的句子，省掉逐句细比。
        need = _SAID * len(core) / 2
        pairs = [core[at:at + 2] for at in range(len(core) - 1)]
        for window, bigrams in zip(self.windows, self.bigrams, strict=True):
            if sum(pair in bigrams for pair in pairs) < need:
                continue
            if _coverage(core, window) >= _SAID:
                return True
        return False

    def closeness(self, sentence: str) -> float:
        """不看数字和英文词，这一句的字最多有几成按顺序对得上这里的一句（或相邻两句）。"""
        core = _sentence_core(sentence)
        if not core:
            return 0.0
        if core in self.core:
            return 1.0
        return max((_coverage(core, window) for window in self.windows), default=0.0)


def _inline_fragment(value: str) -> str:
    """挑出来的几句要拼成一段：段落、列表这类外壳去掉，配不上对的行内标签也去掉，字都留着。"""
    pieces: list[str] = []
    opened: list[tuple[str, int]] = []
    last = 0
    for match in _TAG.finditer(value or ""):
        pieces.append(value[last:match.start()])
        last = match.end()
        closing, name = match.group(1) == "/", match.group(2).lower()
        if name in _VOID and not closing:
            pieces.append(match.group(0))
        elif name in _INLINE and not closing:
            opened.append((name, len(pieces)))
            pieces.append(match.group(0))
        elif name in _INLINE and opened and opened[-1][0] == name:
            opened.pop()
            pieces.append(match.group(0))
        else:
            pieces.append("")
    pieces.append((value or "")[last:])
    for _name, slot in opened:
        pieces[slot] = ""
    return "".join(pieces).strip()


def _novel_zh(extra: str, host: str, context: str = "") -> str:
    """extra 里页面上还没说过的中文句子。

    host 是挂上去的那一对的中文，context 是页面上同一段英文另外几对的中文。收掉的那份
    和留下的译的是同一段英文，两份各自成句：同一句只差标签、标点或几个字时不再挂第二遍，
    带着别处没有的数字、英文词，或者字对不上的句子才挂。
    """
    if not extra or _plain_words(extra) <= _plain_words(host):
        return ""
    parts = [part for part in _ZH_SENTENCE.split(extra) if _sentence_core(part)]
    said = _Said((host or "") + (context or ""))
    novel = [part for part in parts if not said(part)]
    if not novel:
        return ""
    if len(novel) == len(parts):
        return extra
    body = _glue([_inline_fragment(part) for part in novel])
    return f"<p>{body}</p>" if body else ""


def _novel_per_copy(extra: str, shown: Sequence[str]) -> list[str]:
    """收掉的纯文字那份里页面上还没说过的中文，按句挂到带格式那几段后面，每段一份。

    只是数字、英文名不一样的那句挂到字对得上的那一段；哪段都对不上的（留下的那份漏译了）挂在前一句
    所在的那一段后面，前面没有说过的句子就挂第一段有字的那段。漏译的是第一段里的一句，就接在第一段的译文
    后面，不全堆到最后一段。挂上去的是一个段落，原来的列表、引文外壳不带过来；只有图的那几段不挂。
    """
    result = [""] * len(shown)
    if not _novel_zh(extra, "".join(shown)):
        return result
    whole = _Said("".join(shown))
    owners = [_Said(item) for item in shown]
    groups: list[list[str]] = [[] for _ in shown]
    current = next((number for number, item in enumerate(shown) if repeat_key(item)), 0)
    for part in _ZH_SENTENCE.split(extra):
        if not _sentence_core(part):
            continue
        if not whole(part):
            scores = [said.closeness(part) for said in owners]
            best = max(range(len(owners)), key=scores.__getitem__)
            current = best if scores[best] >= _SAID else current
            groups[current].append(part)
            continue
        current = next((number for number, said in enumerate(owners) if said(part)), current)
    for number, group in enumerate(groups):
        body = _glue([_inline_fragment(part) for part in group])
        result[number] = f"<p>{body}</p>" if body else ""
    return result


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
        zh += _novel_zh(extra, zh)
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
        # 导出里一条引文、一个列表项先写成一行，再分几段带格式写一遍：收掉的那一行
        # 译的是好几对的英文，那几对已经显示的中文也算说过。
        context = "".join(
            updated[other].zh or ""
            for other in range(len(updated))
            if other not in drop and other != host and len(keys[other]) >= _SAID_MIN * 2
            and (keys[other] in keys[index] or keys[index] in keys[other])
        )
        extra = _novel_zh(_drop_duplicate_cites(extra, host_zh), host_zh, context)
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


# 中日韩文字、全角标点（。，、；：！？（）「」《》…… 这一类）。弯引号 “” ‘’ 英文里也用，不算。
_CJK = re.compile(
    r"[⺀-⿿　-〿぀-ヿ㄀-ㇿ㐀-䶿一-鿿"
    r"豈-﫿︰-﹏＀-￯]"
)


def _visible_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


# 句末的后引号、后括号和句首的前引号、前括号不算数，看它里面那个字：「好。”」看「。」。
_CLOSERS = "”’\"')]）」』》〉】"
_OPENERS = "“‘\"'([（「『《〈【"


def _glue(parts: Sequence[str]) -> str:
    """几句拼成一段。挨着的两个字有一个是中文或全角标点就直接接上，英文句子之间补一个空格。

    以前一律补空格，中文对照栏和「只看中文」里按句切开译的长段，句号后面都多一个空格（「。 但」）。
    """
    out = ""
    tail = ""
    for part in parts:
        text = _visible_text(part)
        if out:
            joint = tail[-1:] + tail.rstrip(_CLOSERS)[-1:] + text[:1] + text.lstrip(_OPENERS)[:1]
            out += "" if _CJK.search(joint) else " "
        out += part
        if text:
            tail = text
    return out


def _join_html(parts: list[str]) -> str:
    """把同一源段落拆开的叶子拼回去。外壳只留一份，句子之间按 ``_glue`` 接：中文直接接上，英文补一个空格。"""
    pieces = [part.strip() for part in parts if part and part.strip()]
    if not pieces:
        return ""
    if len(pieces) == 1:
        return _strip_src_para(pieces[0])
    opened = [_unwrap_element(part) for part in pieces]
    if any(item is None for item in opened):
        return _strip_src_para(_glue(pieces))
    tag = opened[0][1]
    if any(item[1] != tag for item in opened):
        return _strip_src_para(_glue(pieces))
    inners = [item[2] for item in opened]
    if all(_unwrap_element(inner) for inner in inners):
        inner = _join_html(inners)
    else:
        inner = _glue(inners)
    opening = _strip_src_para(opened[0][0])
    return f"{opening}{inner}</{tag}>"


# 按句切开的同一源段落：相邻、带同一个 data-src-para 的 <p>（中间只隔空白）。一句的内容不跨过 </p>，
# 免得把中间夹着的别的段落也并进来。
_SPLIT_PIECE = r'<p\b[^>]*\bdata-src-para="{group}"[^>]*>(?:(?!</p>).)*</p>'
_SPLIT_RUN = re.compile(
    _SPLIT_PIECE.format(group=r"(\d+)") + r"(?:\s*" + _SPLIT_PIECE.format(group=r"\1") + r")+",
    re.I | re.S,
)
_SPLIT_ONE = re.compile(_SPLIT_PIECE.format(group=r"\d+"), re.I | re.S)


def join_split_paragraphs(translated: str) -> str:
    """整篇读的译文（「只看中文」）：按句切开译的同一源段落并回一段，句子之间按 ``_glue`` 接。

    超过 900 字的多句段落译制时一句一个 ``<p data-src-para="N">``，存下来的译文也是这样，
    「只看中文」以前一句一行。对照页在 ``article_pairs`` 里按原文叶子的分组另并，这里不管。
    ``<br>`` 粘出来的长文没有这个标记，照旧按句留着。并完把 ``data-src-para`` 都去掉。
    """
    merged = _SPLIT_RUN.sub(lambda match: _join_html(_SPLIT_ONE.findall(match.group(0))), translated or "")
    return _strip_src_para(merged)


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


# 导出里写了两遍的引文、列表项、脚注（researchlib.nested_copies）：外层那份里套着这些块的不认。
_COPY_NESTED = frozenset({
    "blockquote", "ul", "ol", "dl", "li", "p", "pre", "table", "div", "figure", "hr", "section", "details",
    "h1", "h2", "h3", "h4", "h5", "h6",
})
_ID_ATTR = re.compile(r"""\s+id=["'][^"']*["']""", re.I)


def _fragment(value: str) -> _El:
    parser = _FragmentParser()
    parser.feed(value or "")
    parser.close()
    return parser.root


def _filled(nodes: Sequence[_El | _Tx]) -> list[_El | _Tx]:
    """去掉空白文字和段落锚点（空的 para-anchor span）以后的节点。"""
    return [
        node for node in nodes
        if not (isinstance(node, _Tx) and not node.data.strip())
        and not (isinstance(node, _El) and node.name == "span" and "para-anchor" in node.start and not node.children)
    ]


def _nested_block(node: _El) -> bool:
    return any(
        isinstance(child, _El) and (child.name in _COPY_NESTED or _nested_block(child)) for child in node.children
    )


def _plain_dl(node: _El | _Tx) -> bool:
    """引文里纯文字那份有一行以「: 」开头（推文里单独一行的链接后面跟着冒号）时，Markdown 把它认成了
    定义列表（线上 22726、22133）：dt、dd 里只有字和段落。这种引文照样算一份纯文字。"""
    if not isinstance(node, _El) or node.name != "dl":
        return False
    for child in _filled(node.children):
        if not isinstance(child, _El) or child.name not in {"dt", "dd"}:
            return False
        for grand in _filled(child.children):
            if isinstance(grand, _El) and grand.name not in _INLINE | _VOID and (
                grand.name != "p" or _nested_block(grand)
            ):
                return False
    return True


def _copy_shape(node: _El | _Tx) -> tuple[str, _El | None]:
    """一个块在导出的两份里能当什么：外层（"quote" 引文、"item" 列表的最后一项）、"plain"（普通段落）、
    "media"（没有字的段落：只有图，或者「[…]」这种只有标点的，夹在带格式那几段中间，不占字），或都不是。

    第二项是外层那份的文字所在的节点（一段的引文是里面那一段，好几段的是整条引文；那个列表项），
    或者那个段落本身。
    """
    if not isinstance(node, _El):
        return "", None
    if node.name == "p":
        return ("plain" if repeat_key(_outer(node)) else "media"), node
    if node.name == "blockquote":
        inner = _filled(node.children)
        if (
            inner
            and all(
                isinstance(child, _El) and (child.name == "p" and not _nested_block(child) or _plain_dl(child))
                for child in inner
            )
            and repeat_key(_outer(node))
        ):
            # 好几段的引文，纯文字那份也分段：整条引文算一个外层，和原文页的 markdown 整块认一样。
            single = len(inner) == 1 and isinstance(inner[0], _El) and inner[0].name == "p"
            return "quote", inner[0] if single else node
        return "", None
    if node.name not in {"ul", "ol"}:
        return "", None
    items = [child for child in _filled(node.children) if isinstance(child, _El)]
    if not items or any(child.name != "li" for child in items) or "tx-list-continuation" in items[-1].start:
        return "", None
    inner = _filled(items[-1].children)
    if inner and all(isinstance(child, _El) and child.name == "p" for child in inner):
        # 这一项是一段或几段（按 <br>、按句落成了几段）：里面不能再套别的块。
        nested = any(_nested_block(child) for child in inner)
    else:
        nested = _nested_block(items[-1])
    return ("", None) if nested else ("item", items[-1])


def _copy_key(kind: str, node: _El | None) -> str:
    return repeat_key(_outer(node)) if kind and node is not None else ""


def _copy_unit(node: _El | _Tx) -> tuple[str, _El | None, str, str]:
    """一个块交给 ``copy_runs`` 的样子：(种类, 外层文字所在的节点, 外层的字, 整块的字)。

    ``_copy_shape`` 认不了的别的引文、列表算 "nested"：不能当外层，可以是外层后面带格式那几块里的一块
    （原文的引文、列表项里套着引文、列表，导出只写一遍）。
    """
    if not isinstance(node, _El):
        return "", None, "", ""
    kind, content = _copy_shape(node)
    whole = repeat_key(_outer(node))
    if not kind and node.name in {"blockquote", "ul", "ol"} and whole:
        kind = "nested"
    return kind, content, _copy_key(kind, content), whole


def _resources(value: str) -> set[str]:
    return _hrefs(value) | {html.unescape(src) for src in _IMG_SRC.findall(value or "")}


def _id_spans(ids: Sequence[str], present: str) -> str:
    """收掉那份上的 id 换成空的 span 留在页面上，链接到这些位置的锚点照样跳得到。"""
    spans = []
    for ident in dict.fromkeys(ids):
        if f'id="{ident}"' not in present and f"id='{ident}'" not in present:
            spans.append(f'<span id="{html.escape(ident, quote=True)}" class="para-anchor"></span>')
    return "".join(spans)


def _pair_shape(en: str) -> tuple[str, _El | None, str, str]:
    """对照的一对按原文那一侧认：(种类, 顶层节点, 外层的字, 整块的字)。当外层的列表外壳里只能有这一项。

    只有图的段落挂在下一段上配成一对（「图、图、一段话」）：都是段落时算一段普通段落，图不占字。
    """
    top = _filled(_fragment(en).children)
    if not top or any(not isinstance(node, _El) for node in top):
        return "", None, "", ""
    if len(top) > 1:
        shapes = [_copy_shape(node) for node in top]
        if any(kind not in {"plain", "media"} for kind, _content in shapes):
            return "", None, "", ""
        key = "".join(_copy_key(kind, content) for kind, content in shapes if kind == "plain")
        return ("plain" if key else "media"), None, key, key
    kind, _content, key, whole = _copy_unit(top[0])
    if kind == "item" and len([child for child in _filled(top[0].children) if isinstance(child, _El)]) != 1:
        kind, key = "nested", ""
    return kind, top[0], key, whole


def _copy_shell(top: _El, kind: str) -> tuple[str, str, str]:
    """把带格式那几段套回去的外壳：(第一段的开头, 后面几段的开头, 结尾)。id 不带，另放空 span。"""
    outer = _ID_ATTR.sub("", top.start)
    if kind == "quote":
        return outer, outer, "</blockquote>"
    item = _ID_ATTR.sub("", [child for child in _filled(top.children) if isinstance(child, _El)][-1].start)
    # 和 layout.units 拆开的多段列表项一样：第二段起是同一项的续段，不再画列表符号。
    if re.search(r"""\bclass=["']""", item):
        more = re.sub(r"""(\bclass=["'])([^"']*)""", r"\1\2 tx-list-continuation", item, count=1)
    else:
        more = item[:-1] + ' class="tx-list-continuation">'
    return outer + item, outer + more, f"</li></{top.name}>"


# 外层按导语、署名切开（``CopyRun.lead``、``trail``）：英文在空白处切，中文在句末、冒号、署名前的破折号处断开。
_EN_CUT = re.compile(r"\s+")
_ZH_CUT = re.compile(r"(?<=[。！？：:])|(?=[—―])")


def _inline_cuts(inner: str, pattern: re.Pattern[str]) -> list[int]:
    """一段只有字和行内标签的 HTML 里能切开的位置：标签外面、不在行内标签中间、``pattern`` 认的地方，
    ``<br>`` 两边也算。里面套着段落、列表这类块的不切（空的）。"""
    points: list[int] = []
    depth = 0
    last = 0
    for match in [*_TAG.finditer(inner), None]:
        end = match.start() if match else len(inner)
        if depth == 0:
            points.extend(found.start() for found in pattern.finditer(inner, last, end))
        if match is None:
            break
        closing, name = match.group(1) == "/", match.group(2).lower()
        if name in _VOID:
            if depth == 0:
                points.extend((match.start(), match.end()))
        elif name not in _INLINE:
            return []
        elif closing:
            depth = max(0, depth - 1)
        else:
            depth += 1
        last = match.end()
    return sorted({point for point in points if 0 < point < len(inner)})


def _text_holder(content: _El) -> _El | None:
    """外层那段字所在的元素，里面只有字和行内标签：段落本身，或者列表项、引文里包着的那一个段落。"""
    inner = _filled(content.children)
    if content.name != "p" and len(inner) == 1 and isinstance(inner[0], _El) and inner[0].name == "p":
        content = inner[0]
    if any(isinstance(child, _El) and child.name not in _INLINE | _VOID for child in content.children):
        return None
    return content


def _split_outer(
    en: _El, zh: _El, target: str, run: CopyRun, shown_zh: str,
) -> tuple[tuple[str, str, str], tuple[str, str, str]] | None:
    """外层按导语、署名切成三截：((英文导语, 英文中间, 英文署名), (中文导语, 中文中间, 中文署名))，
    中间那截收掉，两头留在外层里。切不出来是 None。

    英文按 ``run`` 的字数在空白处切（``nested_copies.plain_ends``）。中文没法按字数切：按句末、冒号、
    署名前的破折号断成几截，每截看带格式那几段的中文里说没说过（``_Said``），在分错的字最少的地方切：
    导语、署名里算说过的、中间那截里算没说过的，字数加起来最少。中间没说过的句子照旧挂到带格式那几段后面
    （``_novel_per_copy``）。英文有导语、中文却切不出来的（译文把导语和引文第一句译成了一句，或者整段都
    对不上），这一处不收，免得导语的译文跟着收掉。
    """
    en_holder, zh_holder = _text_holder(en), _text_holder(zh)
    if en_holder is None or zh_holder is None:
        return None
    en_inner, zh_inner = _serialize(en_holder.children), _serialize(zh_holder.children)
    cut = plain_ends(en_inner, target, run.lead, run.trail, repeat_key, _inline_cuts(en_inner, _EN_CUT))
    if cut is None:
        return None
    english = (en_inner[:cut[0]].strip(), en_inner[cut[0]:cut[1]], en_inner[cut[1]:].strip())
    points = [0, *_inline_cuts(zh_inner, _ZH_CUT), len(zh_inner)]
    pieces = [zh_inner[start:end] for start, end in zip(points, points[1:], strict=False) if start < end]
    said = _Said(shown_zh)
    weights = [len(repeat_key(piece)) for piece in pieces]
    # 每截分到两头时分错的字数（说过的）、分到中间时分错的字数（没说过的）。
    wrong_outside = [weight if weight and said(piece) else 0 for piece, weight in zip(pieces, weights, strict=True)]
    wrong_inside = [weight - outside for weight, outside in zip(weights, wrong_outside, strict=True)]
    if not any(wrong_outside):
        return None
    if run.lead:
        head_end = min(
            range(len(pieces) + 1), key=lambda at: (sum(wrong_outside[:at]) + sum(wrong_inside[at:]), at),
        )
        tail_start = len(pieces)
    else:
        head_end = 0
        tail_start = min(
            range(len(pieces) + 1), key=lambda at: (sum(wrong_inside[:at]) + sum(wrong_outside[at:]), -at),
        )
    # 中间那截挨着导语的一句，开头两个字要在带格式那几段的中文里：译文把导语末尾（「他解释说」）和引文第一句
    # 译成了一句时，这一句整句留在导语里，宁可多显示一遍，也不把导语的字收掉。署名那头同样看结尾两个字。
    shown_core = repeat_key(shown_zh)
    while run.lead:
        first = next((number for number in range(head_end, tail_start) if weights[number]), None)
        if first is None or repeat_key(pieces[first])[:2] in shown_core:
            break
        head_end = first + 1
    while run.trail:
        last = next((number for number in range(tail_start - 1, head_end - 1, -1) if weights[number]), None)
        if last is None or repeat_key(pieces[last])[-2:] in shown_core:
            break
        tail_start = last
    chinese = (
        "".join(pieces[:head_end]).strip(), "".join(pieces[head_end:tail_start]), "".join(pieces[tail_start:]).strip(),
    )
    if (
        (run.lead and not repeat_key(chinese[0])) or (run.trail and not repeat_key(chinese[2]))
        or not any(wrong_outside[head_end:tail_start])
    ):
        return None
    return english, chinese


def _paragraph(value: str) -> str:
    return f"<p>{value}</p>" if value else ""


def _fold_copy_pairs(pairs: list[BilingualPair]) -> list[BilingualPair]:
    """导出里写了两遍的引文、列表项、脚注：按原文页同一条规则（``nested_copies.copy_runs``）收成一份。

    外层那份是一行纯文字，后面紧跟着的几段带着链接、加粗；去掉格式后正好相等时，纯文字那一对不再显示，
    带格式的几对套回引文、列表项的外壳，纯文字那份的段落号留成空锚点。它的中文里页面上还没说过的句子
    挂到字对得上的那一对（``_novel_per_copy``）。它的词、链接、图片要都还在带格式的那几对里，不然不动；
    对不上的重复照旧交给 ``_hide_repeated_pairs``。

    引文里套着的引文自己也写了两遍时，外层要等里面那条收好才接得上：和原文页一样最多收三轮。
    """
    done: set[int] = set()
    for _round in range(3):
        folded = _fold_copy_pairs_once(pairs, done)
        if folded is None:
            break
        pairs, done = folded
    return pairs


# 中文至少这么多个汉字才拿来比译文错位：「是的。」这类短句，两段英文不同、中文一样很常见。
_SHIFT_MIN_HAN = 8
_HAN = re.compile(r"[一-鿿]")


def _leaf_blocks(value: str) -> list[str]:
    """一块里最里层的段落（列表项、引文里的每一段各算一段）。"""
    leaves: list[str] = []

    def walk(node: _El | _Tx) -> None:
        if isinstance(node, _El) and node.name in _COPY_NESTED and _nested_block(node):
            for child in node.children:
                walk(child)
        elif repeat_key(_serialize([node])):
            leaves.append(_serialize([node]))

    for node in _fragment(value).children:
        walk(node)
    return leaves


def _shifted_copies(units: Sequence[tuple[str, str]]) -> bool:
    """带格式那几段的译文错了位：英文不一样（也不互相包含）的两段，中文去掉标签、标点后一字不差。

    ``units`` 是带格式那几段和紧跟在后面的一段，各是 (英文, 中文)，块里有几段就按段比。写两遍的段落一批送去
    译时，模型偶尔漏掉一段、后面的挨个往前挪一格，最后一段补上下一段的译文（线上 21938：三个列表项的中文
    各挪了一格，「优秀的团队遇上糟糕的市场」那句只剩纯文字那份里是对的）。这时收掉纯文字那份就丢了正确的
    译文，这一处不收，两份照原样显示。
    """
    seen: dict[str, str] = {}
    for en, zh in units:
        en_leaves, zh_leaves = _leaf_blocks(en), _leaf_blocks(zh)
        if len(en_leaves) != len(zh_leaves):
            en_leaves, zh_leaves = [en], [zh]
        for en_leaf, zh_leaf in zip(en_leaves, zh_leaves, strict=True):
            en_key, zh_key = repeat_key(en_leaf), repeat_key(zh_leaf)
            if not en_key or len(_HAN.findall(zh_key)) < _SHIFT_MIN_HAN:
                continue
            other = seen.setdefault(zh_key, en_key)
            if other != en_key and other not in en_key and en_key not in other:
                return True
    return False


def _pair_ends(
    pair: BilingualPair, shape: tuple[str, _El | None, str, str], run: CopyRun, shown_zh: str,
) -> tuple[tuple[str, str, str], tuple[str, str, str]] | None:
    """对照页上外层那一对按导语、署名切开（``_split_outer``）。中文那侧要和英文是同一种外层。"""
    kind, top, key, _whole = shape
    zh_top = _filled(_fragment(pair.zh or "").children)
    if top is None or len(zh_top) != 1 or not isinstance(zh_top[0], _El) or zh_top[0].name != top.name:
        return None
    en_kind, en_content = _copy_shape(top)
    zh_kind, zh_content = _copy_shape(zh_top[0])
    if en_kind != kind or zh_kind != kind or en_content is None or zh_content is None:
        return None
    if en_content is top and kind == "quote":
        return None
    return _split_outer(en_content, zh_content, key, run, shown_zh)


def _fold_copy_pairs_once(
    pairs: list[BilingualPair], done: set[int],
) -> tuple[list[BilingualPair], set[int]] | None:
    """收一轮：(收过的对, 套回外壳的那几对的新位置)；没收就是 None。``done`` 是前几轮套回外壳的：
    只能当别的外层带格式那几对里的一对，不再当外层。"""
    from dataclasses import replace

    shapes = [_pair_shape(pair.en or "") for pair in pairs]
    kinds = [
        "nested" if number in done and shape[0] in {"quote", "item"} else shape[0]
        for number, shape in enumerate(shapes)
    ]
    # 好几段的引文在对照页上一段一对、各套一层引文外壳：连着的几对引文合起来当一个外层认（quote_parts）。
    found = copy_runs(kinds, [shape[2] for shape in shapes], [shape[3] for shape in shapes], quote_parts=True)
    placed: set[int] = set()
    updated = list(pairs)
    drop: set[int] = set()
    for run in found:
        outer_numbers, copies = run.outers, run.copies
        outer_en = "".join(pairs[number].en or "" for number in outer_numbers)
        outer_zh = "".join(pairs[number].zh or "" for number in outer_numbers)
        shown = [pairs[number] for number in copies]
        shown_en = "".join(pair.en or "" for pair in shown)
        shown_zh = "".join(pair.zh if pair.zh is not None else (pair.en or "") for pair in shown)
        heads = tails = ("", "")
        if run.lead or run.trail:
            ends = _pair_ends(pairs[outer_numbers[0]], shapes[outer_numbers[0]], run, shown_zh)
            if ends is None:
                continue
            (en_head, outer_en, en_tail), (zh_head, outer_zh, zh_tail) = ends
            heads, tails = (_paragraph(en_head), _paragraph(zh_head)), (_paragraph(en_tail), _paragraph(zh_tail))
        if not (
            _plain_words(outer_en) <= _plain_words(shown_en)
            and _resources(outer_en) <= _resources(shown_en)
            and _resources(outer_zh) <= _resources(shown_en + shown_zh)
        ):
            continue
        after = pairs[copies[-1] + 1:copies[-1] + 2]
        if _shifted_copies([(pair.en or "", pair.zh or "") for pair in shown + after]):
            continue
        kind, top, _key, _whole = shapes[outer_numbers[0]]
        first, more, close = _copy_shell(top, kind)
        extras = _novel_per_copy(
            _drop_duplicate_cites(outer_zh, shown_zh),
            [pair.zh if pair.zh is not None else (pair.en or "") for pair in shown],
        )
        outer_ids = [ident for number in outer_numbers for ident in _element_ids(pairs[number])]
        for position, number in enumerate(copies):
            pair = pairs[number]
            opening = first if position == 0 else more
            en_body, zh = pair.en or "", (pair.zh or "") + extras[position]
            # 导语放在第一段前面，署名放在最后一段后面，都在同一个外壳里。
            if position == 0:
                en_body, zh = heads[0] + en_body, heads[1] + zh
            if position == len(copies) - 1:
                en_body, zh = en_body + tails[0], zh + tails[1]
            en = f"{opening}{en_body}{close}"
            if position == 0:
                present = pair.en + (pair.zh or "") + (pair.anchor or "") + "".join(heads + tails)
                en = _id_spans(outer_ids, present) + en
            updated[number] = replace(pair, en=en, zh=f"{opening}{zh}{close}" if zh else None)
        drop.update(outer_numbers)
        placed.update(copies)
    if not drop:
        return None
    kept = [number for number in range(len(updated)) if number not in drop]
    new_index = {old: new for new, old in enumerate(kept)}
    folded = [
        replace(updated[old], repeat_of=new_index.get(updated[old].repeat_of))
        if updated[old].repeat_of is not None else updated[old]
        for old in kept
    ]
    return folded, {new_index[old] for old in placed | done if old in new_index}


def _same_copy_kind(en: _El | _Tx, zh: _El | _Tx) -> bool:
    """只看中文页按位置收副本：译文里同一位置也得是同一种。普通段落对普通段落，没字的段落（图、「[…]」）
    对没字的段落，套着的引文、列表对同一种有字的块。"""
    kind = _copy_unit(en)[0]
    if not isinstance(en, _El) or not isinstance(zh, _El):
        return False
    if kind == "plain":
        return _copy_shape(zh)[0] == "plain"
    if kind == "media":
        return zh.name == "p" and not repeat_key(_outer(zh))
    return kind in {"quote", "item", "nested"} and zh.name == en.name and bool(repeat_key(_outer(zh)))


def _blocks(nodes: Sequence[_El | _Tx]) -> list[_El | _Tx]:
    return [node for node in nodes if not (isinstance(node, _Tx) and not node.data.strip())]


def _block_name(node: _El | _Tx) -> str:
    # 超过 180 字的标题显示时改回段落（article_layout.readable_headings），英文长、中文短时两边不一样。
    if not isinstance(node, _El):
        return "#text"
    return "p" if re.fullmatch(r"h[1-6]", node.name) else node.name


def _same_skeleton(left: Sequence[_El | _Tx], right: Sequence[_El | _Tx]) -> bool:
    return len(left) == len(right) and all(
        _block_name(a) == _block_name(b) for a, b in zip(left, right, strict=True)
    )


def fold_translated_copies(source: str, translated: str) -> str:
    """只看中文页：原文里写了两遍的引文、列表项、脚注，中文也收成一份。

    在原文上按原文页同一条规则认（``nested_copies.copy_runs``）。译文和原文的顶层块一一对得上
    （块数、标签都一样）才按位置收：纯文字那份的中文不再显示，带格式那几段的中文套回引文、列表项里，
    纯文字那份里页面上还没说过的句子挂在字对得上的那一段后面。段落 id 都留着；哪里对不上，那一处原样留着。
    原文按译制时的切法（materialize 再并句）落成节点比一次：引文里按行、按句切开译的段，译文里是几个
    ``<p>``，原文这样比才对得上；再拿原样的原文比一次（切法改过以前的旧稿是这样译的），哪次收得多用哪次。
    引文里套着的引文自己也写了两遍时，原文和译文一起收，和原文页一样最多三轮。
    """
    from researchlib.translate.provenance import AlignmentError, materialize

    if not translated or ("<blockquote" not in translated and "<li" not in translated):
        return translated
    blocks = _blocks(_fragment(source).children)
    if not _source_copy_runs(blocks, set()) and not _merged_quote_cuts(blocks) and not _merged_list_cuts(blocks):
        return translated
    candidates = []
    try:
        candidates.append(join_split_paragraphs(materialize(source)))
    except AlignmentError:
        pass
    candidates.append(source)
    zh_blocks = _blocks(_fragment(translated).children)
    best, most = translated, 0
    for candidate in candidates:
        en_root = _fragment(candidate)
        if not _same_skeleton(_blocks(en_root.children), zh_blocks):
            continue
        zh_root = _fragment(translated)
        cuts = _merged_quote_cuts(_blocks(en_root.children))
        if cuts:
            en_root, zh_root = _cut_merged_quotes(en_root, zh_root, cuts)
        lists = _merged_list_cuts(_blocks(en_root.children))
        if lists:
            en_root, zh_root = _cut_merged_lists(en_root, zh_root, lists)
        result, count = _fold_translated_rounds(en_root, zh_root, translated)
        if count > most:
            best, most = result, count
    return best


def _merged_quote_cuts(blocks: Sequence[_El | _Tx]) -> dict[int, int]:
    """Python-Markdown 把紧挨着的两条引文并成一条（中间的空行不算断开）。原文的引文里一开头就套着引文时，
    导出的纯文字那份和带格式那几块的第一块（那条套着的引文）就这样并在一起，按顶层块认不出来。
    返回 {第几块: 从第几段拆开}：拆成两条以后能多认出一处的才拆。"""
    units = [_copy_unit(node) for node in blocks]
    kinds = [unit[0] for unit in units]
    keys = [unit[2] for unit in units]
    whole = [unit[3] for unit in units]
    base = len(copy_runs(kinds, keys, whole))
    cuts: dict[int, int] = {}
    for index, node in enumerate(blocks):
        if kinds[index] != "quote" or not isinstance(node, _El):
            continue
        parts = _filled(node.children)
        for cut in range(1, len(parts)):
            first, second = repeat_key(_serialize(parts[:cut])), repeat_key(_serialize(parts[cut:]))
            trial = (
                [*kinds[:index], "quote", "quote", *kinds[index + 1:]],
                [*keys[:index], first, second, *keys[index + 1:]],
                [*whole[:index], first, second, *whole[index + 1:]],
            )
            if len(copy_runs(*trial)) > base:
                cuts[index] = cut
                break
    return cuts


def _cut_merged_quotes(en_root: _El, zh_root: _El, cuts: dict[int, int]) -> tuple[_El, _El]:
    """按 ``_merged_quote_cuts`` 把并在一起的引文拆回两条；译文里同一条引文段数不一样的不拆。"""
    en_blocks = _blocks(en_root.children)
    zh_blocks = _blocks(zh_root.children)
    en_cut: dict[int, int] = {}
    zh_cut: dict[int, int] = {}
    for index, cut in cuts.items():
        en, zh = en_blocks[index], zh_blocks[index]
        if not isinstance(en, _El) or not isinstance(zh, _El) or zh.name != "blockquote":
            continue
        en_parts, zh_parts = _filled(en.children), _filled(zh.children)
        if len(zh_parts) != len(en_parts):
            continue
        en_cut[id(en)] = en.children.index(en_parts[cut])
        zh_cut[id(zh)] = zh.children.index(zh_parts[cut])

    def rebuild(root: _El, at: dict[int, int]) -> _El:
        out = []
        for node in root.children:
            if isinstance(node, _El) and id(node) in at:
                split = at[id(node)]
                out.append(
                    f"{node.start}{_serialize(node.children[:split])}</{node.name}>"
                    f"<{node.name}>{_serialize(node.children[split:])}</{node.name}>"
                )
            else:
                out.append(_serialize([node]))
        return _fragment("".join(out))

    if not en_cut or len(en_cut) != len(zh_cut):
        return en_root, zh_root
    return rebuild(en_root, en_cut), rebuild(zh_root, zh_cut)


def _list_items(node: _El | _Tx) -> list[_El]:
    """一个无序列表的各项；不是只有列表项的无序列表就是空的。"""
    if not isinstance(node, _El) or node.name != "ul":
        return []
    items = [child for child in _filled(node.children) if isinstance(child, _El)]
    return items if all(item.name == "li" for item in items) else []


def _merged_list_cuts(blocks: Sequence[_El | _Tx]) -> dict[int, int]:
    """Python-Markdown 把紧挨着的列表项并成一个列表。导出写的带格式那几项（原文里是嵌套列表的项）后面
    紧跟着正文自己的列表项时，或者纯文字那份所在的项后面紧跟着它带格式的那几项时，它们就并在一个列表里，
    按顶层块认不出来（线上 21068、22704）。返回 {第几块: 从第几项拆开}：拆成两个列表以后能多认出一处的
    才拆，取认出最多的那一刀。只看无序列表：有序列表拆开，后一半的序号会重新数。"""
    units = [_copy_unit(node) for node in blocks]
    kinds = [unit[0] for unit in units]
    keys = [unit[2] for unit in units]
    whole = [unit[3] for unit in units]
    targets = [key for kind, key in zip(kinds, keys, strict=True) if kind in {"quote", "item"} and key]
    base = None
    cuts: dict[int, int] = {}
    for index, node in enumerate(blocks):
        items = _list_items(node)
        if len(items) < 2 or not isinstance(node, _El):
            continue
        item_keys = [repeat_key(_outer(item)) for item in items]
        best = None
        for cut in range(1, len(items)):
            head = "".join(item_keys[:cut])
            # 只试拆完接得上的：前一截是某个外层的字里的一段，或者前一截最后一项的字里有后一截的第一项。
            if not (
                (head and any(head in target for target in targets))
                or (item_keys[cut] and item_keys[cut] in item_keys[cut - 1])
            ):
                continue
            parts = [
                _filled(_fragment(f"{node.start}{_serialize(items[:cut])}</ul>").children)[0],
                _filled(_fragment(f"<ul>{_serialize(items[cut:])}</ul>").children)[0],
            ]
            split = [_copy_unit(part) for part in parts]
            count = len(copy_runs(
                [*kinds[:index], *(unit[0] for unit in split), *kinds[index + 1:]],
                [*keys[:index], *(unit[2] for unit in split), *keys[index + 1:]],
                [*whole[:index], *(unit[3] for unit in split), *whole[index + 1:]],
            ))
            if base is None:
                base = len(copy_runs(kinds, keys, whole))
            if count > (base if best is None else best[0]):
                best = (count, cut)
        if best is not None:
            cuts[index] = best[1]
    return cuts


def _cut_merged_lists(en_root: _El, zh_root: _El, cuts: dict[int, int]) -> tuple[_El, _El]:
    """按 ``_merged_list_cuts`` 把并在一起的列表拆成两个；译文里同一个列表项数不一样的不拆。"""
    en_blocks = _blocks(en_root.children)
    zh_blocks = _blocks(zh_root.children)
    at: dict[int, int] = {}
    for index, cut in cuts.items():
        en, zh = en_blocks[index], zh_blocks[index]
        if len(_list_items(zh)) != len(_list_items(en)):
            continue
        at[id(en)] = at[id(zh)] = cut

    def rebuild(root: _El) -> _El:
        out = []
        for node in root.children:
            if isinstance(node, _El) and id(node) in at:
                items = _list_items(node)
                cut = at[id(node)]
                out.append(f"{node.start}{_serialize(items[:cut])}</ul><ul>{_serialize(items[cut:])}</ul>")
            else:
                out.append(_serialize([node]))
        return _fragment("".join(out))

    if not at:
        return en_root, zh_root
    return rebuild(en_root), rebuild(zh_root)


def _fold_translated_rounds(en_root: _El, zh_root: _El, translated: str) -> tuple[str, int]:
    """最多收三轮：(收过的译文, 一共收了几处)。"""
    result = translated
    done: set[int] = set()
    count = 0
    for _round in range(3):
        folded = _fold_translated_once(en_root, zh_root, done)
        if folded is None:
            break
        en_html, result, done, folds = folded
        count += folds
        en_root, zh_root = _fragment(en_html), _fragment(result)
        if not _same_skeleton(_blocks(en_root.children), _blocks(zh_root.children)):
            break
    return result, count


def _source_copy_runs(blocks: Sequence[_El | _Tx], done: set[int]) -> list[CopyRun]:
    """原文的顶层块交给 ``copy_runs``。``done`` 是前几轮放回去的外层：只能当别的外层带格式那几块里的一块。"""
    units = [_copy_unit(node) for node in blocks]
    kinds = [
        "nested" if number in done and unit[0] in {"quote", "item"} else unit[0] for number, unit in enumerate(units)
    ]
    return copy_runs(kinds, [unit[2] for unit in units], [unit[3] for unit in units])


def _put_back(holder: _El, content: _El, kind: str, body: str) -> str:
    """带格式那几段放回外层：好几段的引文整个换掉，一段的引文换掉里面那段，列表项换掉这一项的内容。"""
    if content is holder:
        return f"{holder.start}{body}</{holder.name}>"
    middle = body if kind == "quote" else f"{content.start}{body}</li>"
    at = holder.children.index(content)
    return (
        f"{holder.start}{_serialize(holder.children[:at])}{middle}"
        f"{_serialize(holder.children[at + 1:])}</{holder.name}>"
    )


def _fold_translated_once(
    en_root: _El, zh_root: _El, done: set[int],
) -> tuple[str, str, set[int], int] | None:
    """收一轮：返回 (收过的原文, 收过的译文, 放回去的外层在新的顶层块里排第几, 这一轮收了几处)。原文也收，
    下一轮还按位置对得上；这一轮没收就是 None。"""
    en_blocks = _blocks(en_root.children)
    zh_blocks = _blocks(zh_root.children)
    en_rebuilt: dict[int, str] = {}
    zh_rebuilt: dict[int, str] = {}
    placed = set(done)
    removed_numbers: set[int] = set()
    for run in _source_copy_runs(en_blocks, done):
        index, copies = run.outers[0], run.copies
        holder, en_holder = zh_blocks[index], en_blocks[index]
        kind, content = _copy_shape(holder)
        en_kind, en_content = _copy_shape(en_holder)
        if (
            kind != en_kind or content is None or en_content is None
            or not isinstance(holder, _El) or not isinstance(en_holder, _El)
            or (content is holder) != (en_content is en_holder)
            or any(not _same_copy_kind(en_blocks[number], zh_blocks[number]) for number in copies)
            or len(_filled(holder.children)) != len(_filled(en_holder.children))
        ):
            continue
        pieces = [_outer(zh_blocks[number]) for number in copies]
        shown = "".join(pieces)
        whole = content is holder
        removed = _serialize(_filled(holder.children)) if whole else _outer(content)
        ids_from = _serialize(holder.children) if whole else removed
        english, chinese = ("", "", ""), ("", "", "")
        if run.lead or run.trail:
            # 导语、署名留在外层里：只收中间那截。
            ends = None if whole else _split_outer(en_content, content, _copy_key(en_kind, en_content), run, shown)
            if ends is None:
                continue
            english, chinese = ends
            removed = chinese[1]
        if not _resources(removed) <= _resources(shown):
            continue
        around = [*copies, copies[-1] + 1] if copies[-1] + 1 < len(en_blocks) else copies
        if _shifted_copies([(_serialize([en_blocks[number]]), _serialize([zh_blocks[number]])) for number in around]):
            continue
        extras = _novel_per_copy(_drop_duplicate_cites(removed, shown), pieces)
        spans = _id_spans(re.findall(r"""\bid=["']([^"']+)["']""", ids_from), shown + chinese[0] + chinese[2])
        body = (
            _paragraph(chinese[0]) + spans + "".join(piece + more for piece, more in zip(pieces, extras, strict=True))
            + _paragraph(chinese[2])
        )
        zh_rebuilt[id(holder)] = _put_back(holder, content, kind, body)
        en_rebuilt[id(en_holder)] = _put_back(
            en_holder, en_content, kind,
            _paragraph(english[0]) + "".join(_outer(en_blocks[number]) for number in copies) + _paragraph(english[2]),
        )
        for number in copies:
            zh_rebuilt[id(zh_blocks[number])] = ""
            en_rebuilt[id(en_blocks[number])] = ""
        placed.add(index)
        removed_numbers.update(copies)
    if not zh_rebuilt:
        return None

    def rebuild(root: _El, rebuilt: dict[int, str]) -> str:
        return "".join(rebuilt[id(node)] if id(node) in rebuilt else _serialize([node]) for node in root.children)

    kept = [number for number in range(len(en_blocks)) if number not in removed_numbers]
    new_index = {old: new for new, old in enumerate(kept)}
    return (
        rebuild(en_root, en_rebuilt), rebuild(zh_root, zh_rebuilt),
        {new_index[number] for number in placed if number in new_index}, len(placed - done),
    )


# 连着两遍的同一张图也往引文、列表里看（副本收回引文以后，夹在里面的图在那里连着）。
_IMAGE_PARENTS = frozenset({"blockquote", "ul", "ol", "li"})


def _image_srcs(node: _El | _Tx) -> tuple[str, ...]:
    if not isinstance(node, _El) or _copy_shape(node)[0] != "media":
        return ()
    return tuple(html.unescape(src) for src in _IMG_SRC.findall(_outer(node)))


def _repeated_image_paths(parent: _El, path: tuple[int, ...] = ()) -> list[tuple[int, ...]]:
    """只有图的段落和紧挨着的上一块是同几张图（地址一样）：后面那份的位置（逐层第几块）。"""
    found: list[tuple[int, ...]] = []
    previous: tuple[str, ...] = ()
    for number, node in enumerate(_blocks(parent.children)):
        srcs = _image_srcs(node)
        if srcs and srcs == previous:
            found.append((*path, number))
            continue
        previous = srcs
        if isinstance(node, _El) and node.name in _IMAGE_PARENTS:
            found.extend(_repeated_image_paths(node, (*path, number)))
    return found


def _node_at(root: _El, path: Sequence[int]) -> _El | None:
    node: _El | _Tx = root
    for number in path:
        children = _blocks(node.children) if isinstance(node, _El) else []
        if number >= len(children):
            return None
        node = children[number]
    return node if isinstance(node, _El) else None


def _without(root: _El, nodes: Sequence[_El]) -> str:
    """去掉这几个节点；它们身上别处没有的 id 换成空 span 留在原处，链到那里的锚点照样跳得到。"""
    for node in nodes:
        node.replacement = ""
    present = _serialize(root.children)
    for node in nodes:
        node.replacement = _id_spans(re.findall(r"""\bid=["']([^"']+)["']""", node.start), present)
    return _serialize(root.children)


def drop_repeated_images(value: str) -> str:
    """导出把同一张图连着写了两遍：只看中文页上只留一张。原文页在 markdown 里收（nested_copies），对照页按对收。"""
    if not value or value.count("<img") < 2:
        return value
    root = _fragment(value)
    paths = _repeated_image_paths(root)
    if not paths:
        return value
    return _without(root, [node for node in (_node_at(root, path) for path in paths) if node is not None])


def _drop_pair_repeated_images(pair: BilingualPair) -> BilingualPair:
    """对照页的一对里同一张图连着两遍：英文那侧去掉后一份，中文那侧同一位置是空段落或图的也去掉。"""
    from dataclasses import replace

    if (pair.en or "").count("<img") < 2:
        return pair
    en_root = _fragment(pair.en)
    paths = _repeated_image_paths(en_root)
    if not paths:
        return pair
    en = _without(en_root, [node for node in (_node_at(en_root, path) for path in paths) if node is not None])
    if not pair.zh:
        return replace(pair, en=en)
    zh_root = _fragment(pair.zh)
    blank = [
        node for node in (_node_at(zh_root, path) for path in paths)
        if node is not None and node.name == "p" and not repeat_key(_outer(node))
    ]
    return replace(pair, en=en, zh=_without(zh_root, blank) if blank else pair.zh)


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
            # 有回执（mapped）的译文，每段的身份和资源写入时已经逐段核过，数字串段也在写入时拦了。
            # 这条按字面数字猜位置，只给没有回执的旧稿用：中文常把「in 2015」挪到「增长10%」前面，
            # 拿来查新译文，一段语序调整就会让整页退成两栏。
            elif state.status != "mapped" and positional_numbers_conflict(
                [a.html for a in left], [b.html for b in right],
            ):
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
        # 导出写了两遍的引文、列表项、脚注先按原文页同一条规则收成一份，连着两遍的同一张图只留一张，
        # 剩下的长重复再交给 _hide_repeated_pairs。
        result = replace(result, pairs=[_drop_pair_repeated_images(pair) for pair in _fold_copy_pairs(result.pairs)])
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
    "drop_repeated_images",
    "fold_translated_copies",
    "join_split_paragraphs",
    "leaf_projection_gaps",
    "projection_gaps",
    "protected_content_gaps",
]

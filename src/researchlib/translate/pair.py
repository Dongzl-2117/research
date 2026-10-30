"""把原文 HTML 与译文 HTML 配成中英对照。

新译文按叶子段原位写回，和 AIHOT segmentsOf 一样：blockquote 里还有块级子节点时，
段是里面的 p / 标题 / li，不是整段引用。多条或很长的定义列表引用会再拆进 dt / dd。
这里按段的位置并排。
旧稿若只在中文侧删过连续重复段，两边再走一遍同一套去重，把同段拉齐。
叶子数已经一样时按位置收：后一对的英文是前面某对的长段近重复，且中文也是那一对的
同一段（译文更短也算），后一对收掉。更短的一句已经整句包含在另一对更长的英文里时，
收掉短的那一对，长的留下。开头已经占了较短那句大约四分之三、长度又接近、中文是同一段的
另一种说法时，也收掉短的。英文整句相同但中文是另一句的留下。
段数仍对不齐也不整页退回中文：多出来的挂在末尾。
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
    只共享开头、后文不同的两段都留着。叶子数已经对齐时不用这条单边收，改走
    ``_drop_aligned_repeats``，免得中文更短时条数对不齐、整页重复都留着。
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


def _contained_text(html: str) -> str:
    """可见文字，只压空白并忽略大小写。标点留着，括号不同的两条来源注释不会变成包含。

    标签换成空格再压空白。``<strong>`` 紧挨句号时，和正文里句号前多一个空格是同一长度。
    """
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _display_long(html: str) -> bool:
    """对照页上够不够大约 80 字。和 ``repeat_long`` 同一道门槛，但标签边界按空格计。"""
    return len(_contained_text(html)) >= 80


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


def _drop_aligned_repeats(
    left: list[Segment], right: list[Segment]
) -> tuple[list[Segment], list[Segment]]:
    """叶子已经按位置对齐时，把重复的后一对整对收掉。

    英文可见文字大约 80 字以上，和前面某一对相同、只是它的开头、或几乎一样
    （长度差在一成以内、相似度 0.96 以上），并且中文也是那一对的同一段译文。
    标签边界按空格计，句号前多一个空格和 ``<strong>`` 贴着句号算同样长。中文措辞
    不同但共享一长串时也算同一段。隔了几段再贴出来的同一句同样收，不要求紧挨着。
    英文重复但中文是另一句的留下，避免把那句译文丢掉，后面的段落因此错位。
    """
    if len(left) != len(right):
        return left, right
    priors: dict[str, list[tuple[str, str]]] = {}
    kept_left: list[Segment] = []
    kept_right: list[Segment] = []
    for en, zh in zip(left, right, strict=True):
        en_key = _leaf_key(en.html)
        zh_key = _leaf_key(zh.html)
        if _display_long(en.html):
            bucket = en_key[:48]
            repeated = any(
                same_repeat(prev_en, en_key) and _same_lineage(prev_zh, zh_key)
                for prev_en, prev_zh in priors.get(bucket, ())
            )
            if repeated:
                continue
            priors.setdefault(bucket, []).append((en_key, zh_key))
        kept_left.append(en)
        kept_right.append(zh)
    kept_left, kept_right = _drop_contained_pairs(kept_left, kept_right)
    return _drop_near_prefixes(kept_left, kept_right)


def _drop_contained_pairs(
    left: list[Segment], right: list[Segment]
) -> tuple[list[Segment], list[Segment]]:
    """短的长英文整句已经出现在另一对更长的英文里时，收掉短的那一对。

    长的那对留着，所以后面多出来的句子还在。引用按句切开、正文仍是一整段时，
    切开的句子会整句包含在那一段里；两遍中文往往不是逐字嵌套，仍然收短的。
    英文整句相同、只是中文不同的两对一样长，不在这里收。
    """
    texts = [_contained_text(segment.html) for segment in left]
    drop: set[int] = set()
    for index, short in enumerate(texts):
        if not _display_long(left[index].html):
            continue
        for other, long in enumerate(texts):
            if other == index or len(long) <= len(short):
                continue
            if short in long:
                drop.add(index)
                break
    if not drop:
        return left, right
    kept = [index for index in range(len(left)) if index not in drop]
    return [left[index] for index in kept], [right[index] for index in kept]


def _shared_prefix_len(left: str, right: str) -> int:
    count = 0
    for char_left, char_right in zip(left, right, strict=False):
        if char_left != char_right:
            break
        count += 1
    return count


def _drop_near_prefixes(
    left: list[Segment], right: list[Segment]
) -> tuple[list[Segment], list[Segment]]:
    """较短的一句有大约四分之三是较长那句的开头，而且两句长度接近时，收掉短的。

    322 字和 352 字的自我介绍是这种：不是整句包含，相似度也到不了 0.96。中文得是
    同一段的另一种说法。只共享大约三分之二开头、后文明显岔开的两份提示不收。
    长的那对留下，所以多出来的那一截还在。
    """
    texts = [_contained_text(segment.html) for segment in left]
    zh_keys = [_leaf_key(segment.html) for segment in right]
    drop: set[int] = set()
    for index, short in enumerate(texts):
        if len(short) < 80:
            continue
        for other, long in enumerate(texts):
            if other == index or len(long) <= len(short):
                continue
            if (len(long) - len(short)) / len(long) > 0.12:
                continue
            if _shared_prefix_len(short, long) < len(short) * 0.75:
                continue
            short_zh, long_zh = zh_keys[index], zh_keys[other]
            if not short_zh:
                drop.add(index)
                break
            if long_zh and _same_lineage(long_zh, short_zh):
                drop.add(index)
                break
    if not drop:
        return left, right
    kept = [index for index in range(len(left)) if index not in drop]
    return [left[index] for index in kept], [right[index] for index in kept]


def _show(segment: Segment) -> tuple[str, str | None]:
    """去掉段上的段落号。叶子本身没有 id 时，用切段时记下的祖先 id。"""
    html, anchor = _take_anchor(segment.html)
    return html, anchor or segment.anchor


def _best_blocks(
    original_html: str, zh_html: str, *, split_br: bool, split_sentences: bool
) -> tuple[list[Segment], list[Segment]] | None:
    """按一种切法选两边展示用的叶子。先原样，再看同一套去重。

    叶子数已经一样时按位置收重复对：英文够长并且近重复，中文是同一段（更短也算）
    才整对丢掉。短句整句包含在更长的一段里时，收掉短句，长段留下。两边各收各的会因为
    中文不到 80 字而对不齐条数，整页重复都留着。
    连续相同的块先去掉，再按位置收一次，短的连续重复和隔开的长重复一起收掉。
    叶子数本来就不一样时，两边仍各收各的长叶子，只有差距变小才改用那一份。
    只短一边不硬配。
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
    if raw_gap == 0 and len(kids_o) == len(kids_t):
        candidates = [_drop_aligned_repeats(kids_o, kids_t)]
        if block_o and block_t and len(block_o) == len(block_t):
            candidates.append(_drop_aligned_repeats(block_o, block_t))
    else:
        candidates = [
            (block_o, block_t),
            (_drop_repeated_leaves(kids_o), _drop_repeated_leaves(kids_t)),
            (_drop_repeated_leaves(block_o), _drop_repeated_leaves(block_t)),
        ]
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

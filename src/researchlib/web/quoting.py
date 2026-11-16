"""文章页：把引用的那一块在正文里找出来，标上高亮，打开时滚到那里。

问库的 [n]、检索结果、观点原句都链到 /articles/{id}?chunk={块号}。原来落在文章顶部，再靠顶上的
「引用的段落」框自己往下找；现在在渲染好的正文里按文字找到这一块覆盖的段落，加上 is-quoted。
按渲染后的 HTML 找，不预先存段落编号：图片后来下载到本地、只有图的段落编号会变，存下的编号就对不上了。
找不到（比如块里的字和渲染出的段落差太多）就还用顶上的框。
"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from collections.abc import Callable

from researchlib.translate.segments import top_level_blocks

QUOTED_CLASS = "is-quoted"
_TAG = re.compile(r"<[^>]+>")
_OPEN = re.compile(r"^<([a-z0-9]+)\b([^>]*)>", re.I)
_CLASS = re.compile(r'\bclass="([^"]*)"', re.I)
_KEY = 24


def _norm(text: str) -> str:
    # 保留符号、大小写及兼容字符的语义：>／<、1/23／12/3、m／M、²／2 都不能混同。
    # 仅收起排版空白；数字之间的空白仍留边界，避免 1 23 和 12 3 被粘成同一个数。
    text = unicodedata.normalize("NFC", html_lib.unescape(text or "")).replace("−", "-")
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"(?<!\d) | (?!\d)", "", text)


def _add_class(block: str) -> str:
    match = _OPEN.match(block)
    if not match:
        return f'<div class="{QUOTED_CLASS}">{block}</div>'
    tag, attrs = match.group(1), match.group(2)
    if _CLASS.search(attrs):
        attrs = _CLASS.sub(lambda m: f'class="{m.group(1)} {QUOTED_CLASS}"', attrs, count=1)
    else:
        attrs = f'{attrs} class="{QUOTED_CLASS}"'
    return f"<{tag}{attrs}>{block[match.end():]}"


def _reach(text: str, target: str, position: int) -> int | None:
    """这一段接在块里 position 处，接上以后吃到哪。接不上返回 None。"""
    rest = target[position:]
    if text.startswith(rest):
        return len(target)  # 块在这一段中间结束
    if not target.startswith(text, position):
        return None
    return position + len(text)


def _follow(blocks: list[str], norms: list[str], target: str, start: int, consumed: int) -> tuple[list[int], int]:
    """从 start 这一段顺着往下吃：后面的段落文字接得上块里剩下的字就算上，直到块吃完。返回 (段落, 吃到哪)。"""
    covered = [start]
    position = consumed
    for index in range(start + 1, len(blocks)):
        if position >= len(target):
            break
        text = norms[index]
        if (not text or text in {"外部图片已省略", "[外部图片已省略]"}
                or re.fullmatch(r"\[[^\[\]]*缺图\]", text)):
            continue  # 只有图（或缺图占位）的段落：块里没有它的字，跳过，接着往下看
        reached = _reach(text, target, position)
        if reached is None:
            # 导出时写了两遍的引文、列表项，页面上收成了一份（nested_copies），块里还是两遍：
            # 紧接着又是刚标上的那段时，跳过这一遍再接。
            last = norms[covered[-1]]
            if len(last) >= _KEY and target.startswith(last, position):
                reached = _reach(text, target, position + len(last))
        if reached is None:
            break
        covered.append(index)
        position = reached
    return covered, position


def _exact(blocks: list[str], norms: list[str], target: str) -> list[list[int]]:
    """一字不差（归一以后）对上的位置，每处是它覆盖的段落。"""
    head = target[:_KEY]
    matches: list[list[int]] = []
    for index, text in enumerate(norms):
        if len(text) < 4:
            continue
        # 这一段就是块的开头，或者块从这一段中间开始（长段落被切成了几块）。
        if target in text:
            covered, position = [index], len(target)
        elif target.startswith(text):
            consumed = len(text)
            covered, position = _follow(blocks, norms, target, index, consumed)
        else:
            found = text.find(head)
            if found < 0 or not target.startswith(text[found:]):
                continue
            consumed = len(text) - found
            covered, position = _follow(blocks, norms, target, index, consumed)
        if position != len(target):
            # 共同前缀不足以确认，必须核对整块。否则保留页面顶部的准确引用框。
            continue
        if covered not in matches:
            matches.append(covered)
    return matches


# ---- 容小差异的对齐 ----------------------------------------------------------------------------
# 分块的字和渲染出的页面差几个字是常事：有序列表的序号（块里「1.」，页面是 <ol>）、[!NOTE] 标记、
# 落单的 *、归档头那一行、说话人的写法（块里「作者答：」，页面「方伟：」）。内容都在页面上，
# 一字不差的规则却整块放弃，读者落在页面顶上。这里按顺序对齐：块里八成以上的字落在 8 字以上的
# 连续相同段里，差出来的每一处都只是排版字符或几个字的称呼，才算找到。
# 数字一个也不容：差出来的地方带数字、或者紧挨着数字（10m／10M、>10%／<10%、8 亿／8%），就不算。
_MIN_RUN = 8          # 少于这么多字的相同段不算对上，免得零星的字凑数
_COVER = 0.8          # 块里至少这么多字要对上
_PROBE = 16           # 探针长度：拿块里几处这么长的字去页面里定位
_PROBES = 9           # 探针个数
_PER_PROBE = 12       # 每根探针在页面里最多看几处（常见短语别把候选撑爆）
_GAP_WORDS = 6        # 一处差异里，去掉排版字符以后最多容几个字
_GAP_TOTAL = 12       # 全块差异加起来最多容几个字（再按块长放宽，见 _gap_budget）
_FORMAT_CHARS = set(" *_#>`~|[]()!-—–•·.,，。、：:;；!?！？“”\"'‘’「」『』《》〈〉（）【】…\u3000")
_ADMONITION = re.compile(r"\[!?(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]?", re.I)
_PATHISH = re.compile(r"[A-Za-z0-9_./\\-]*\.(?:html?|md|pdf|png|jpe?g|gif|webp)\b", re.I)
# 块里有、页面上不显示的东西：先从块里去掉再对齐。只认写死的样子，别把正文吃掉。
_NOISE = re.compile(
    r"\[外部图片已省略\]|\[[^\[\]]{0,40}缺图\]|\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]"
    r"|(?:截屏|截图|屏幕快照|Screen ?Shot)?[A-Za-z0-9_. -]{0,40}\.(?:png|jpe?g|gif|webp)"
    r"|原文链接：https?://[A-Za-z0-9./_?=&%#~:+-]+|原始HTML：[A-Za-z0-9./_-]+",
    re.I,
)
_ORDINAL = re.compile(r"(?<![0-9.])[(（]?[0-9]{1,3}[.、．)）](?![0-9])")


def _digitish(char: str) -> bool:
    return bool(char) and (char.isdigit() or char in "%％‰")


def _meaningful(gap: str) -> str:
    gap = _ADMONITION.sub("", gap)
    gap = _PATHISH.sub("", gap)
    return "".join(char for char in gap if char not in _FORMAT_CHARS)


def _gap_ok(gap: str, before: str, after: str, *, allow_ordinals: bool) -> tuple[bool, int]:
    """一处对不上的字能不能容：返回 (能不能，算几个字)。"""
    if not gap:
        return True, 0
    if allow_ordinals and not _digitish(before) and not _digitish(after):
        # 唯一能容的数字是块里有、页面上渲染成 <ol> 的列表序号（「1.」「2、」），前后都不挨着数字。
        gap = _ORDINAL.sub("", gap)
        if not gap:
            return True, 0
    if any(_digitish(char) for char in gap) or _digitish(before) or _digitish(after):
        return False, 0
    words = _meaningful(gap)
    return len(words) <= _GAP_WORDS, len(words)


def _gap_in(gap: str, target: str, start: int, end: int, window: str) -> tuple[bool, int, int]:
    """块里没对上的一处：返回 (能不能容, 算几个字, 其中页面上别处已有的字数)。

    导出时写了两遍的引文、列表项，页面上收成了一份（nested_copies）：块里第二遍的字在窗口里别处已经有，
    算对上；剩下的零头照样只容排版字符和几个字的称呼。
    """
    before = target[start - 1] if start else ""
    after = target[end] if end < len(target) else ""
    if len(gap) < _KEY:
        ok, cost = _gap_ok(gap, before, after, allow_ordinals=True)
        return ok, cost, 0
    from difflib import SequenceMatcher

    runs = [m for m in SequenceMatcher(None, gap, window, autojunk=False).get_matching_blocks() if m.size >= _KEY]
    again = sum(run.size for run in runs)
    if not again:
        ok, cost = _gap_ok(gap, before, after, allow_ordinals=True)
        return ok, cost, 0
    spent = 0
    edges = [0] + [run.a + run.size for run in runs]
    starts = [run.a for run in runs] + [len(gap)]
    for left, right in zip(edges, starts, strict=True):
        piece = gap[left:right]
        if not piece:
            continue
        ok, cost = _gap_ok(piece, gap[left - 1] if left else before, gap[right] if right < len(gap) else after,
                           allow_ordinals=True)
        if not ok:
            return False, 0, 0
        spent += cost
    return True, spent, again


def _gap_budget(target: str) -> int:
    return max(_GAP_TOTAL, len(target) // 25)


def _candidates(joined: str, target: str) -> list[int]:
    """块大概从页面文字的哪里开始：几根探针各自找到的位置往回推，按位置聚成几处，票多的在前。"""
    if len(target) <= _PROBE:
        probes = [0]
    else:
        span = len(target) - _PROBE
        probes = sorted({round(span * index / (_PROBES - 1)) for index in range(_PROBES)})
    votes: list[int] = []
    for offset in probes:
        needle = target[offset:offset + _PROBE]
        position = joined.find(needle)
        seen = 0
        while position >= 0 and seen < _PER_PROBE:
            votes.append(position - offset)
            seen += 1
            position = joined.find(needle, position + 1)
    if not votes:
        return []
    votes.sort()
    clusters: list[list[int]] = [[votes[0]]]
    for vote in votes[1:]:
        if vote - clusters[-1][-1] <= 64:
            clusters[-1].append(vote)
        else:
            clusters.append([vote])
    clusters.sort(key=lambda group: -len(group))
    need = 1 if len(probes) <= 2 else 2
    return [max(0, group[len(group) // 2]) for group in clusters[:6] if len(group) >= need]


def _align(joined: str, target: str, start: int) -> list[tuple[int, int]] | None:
    """在 start 附近按顺序对齐。对得上返回页面上对上的各段 (起, 止)；对不上返回 None。"""
    from difflib import SequenceMatcher

    slack = max(64, len(target) // 2)
    low = max(0, start - slack)
    high = min(len(joined), start + len(target) + slack)
    window = joined[low:high]
    matcher = SequenceMatcher(None, target, window, autojunk=False)
    runs = [m for m in matcher.get_matching_blocks() if m.size >= _MIN_RUN]
    if not runs:
        return None
    covered = sum(run.size for run in runs)
    spent = 0
    # 相邻两段相同的字之间，块里一处、页面上一处：合起来算一处差异，按多的那边算字数。
    # 块的头尾没对上的也核（序号、标记、称呼）；页面在窗口两头多出来的字不算。
    bounds = [(0, None)] + [(run.a + run.size, run.b + run.size) for run in runs]
    nexts = [(run.a, run.b) for run in runs] + [(len(target), None)]
    for (a_end, b_end), (a_next, b_next) in zip(bounds, nexts, strict=True):
        gap = target[a_end:a_next]
        cost = 0
        if b_end is not None and b_next is not None:
            extra = window[b_end:b_next]
            ok, cost = _gap_ok(extra, window[b_end - 1] if b_end else "",
                               window[b_next] if b_next < len(window) else "", allow_ordinals=False)
            if not ok:
                return None
        if gap:
            ok, gap_cost, again = _gap_in(gap, target, a_end, a_next, window)
            if not ok:
                return None
            covered += again
            cost = max(cost, gap_cost)
        spent += cost
    if spent > _gap_budget(target) or covered < _COVER * len(target):
        return None
    return [(low + run.b, low + run.b + run.size) for run in runs]


def _fuzzy(norms: list[str], target: str) -> list[list[int]]:
    offsets = []
    position = 0
    for text in norms:
        offsets.append(position)
        position += len(text)
    joined = "".join(norms)
    found: list[list[int]] = []
    for start in _candidates(joined, target):
        spans = _align(joined, target, start)
        if spans is None:
            continue
        hits = [0] * len(norms)
        for begin, end in spans:
            index = max(0, _block_at(offsets, begin))
            while index < len(norms) and offsets[index] < end:
                hits[index] += max(0, min(end, offsets[index] + len(norms[index])) - max(begin, offsets[index]))
                index += 1
        # 一段里对上的字够多才标：半段以上，或者够一句话（长段落被切成几块时，块只占它一部分）。
        covered = [index for index, hit in enumerate(hits)
                   if hit and hit >= min(_KEY, len(norms[index]) / 2)]
        if covered and not any(set(covered) & set(other) for other in found):
            found.append(covered)
    return found


def _block_at(offsets: list[int], position: int) -> int:
    from bisect import bisect_right

    return bisect_right(offsets, position) - 1


def _norms(blocks: list[tuple[int, str]]) -> list[str]:
    norms = []
    for _part, block in blocks:
        text = _norm(_TAG.sub("", block))
        if text in {"外部图片已省略", "[外部图片已省略]"} or re.fullmatch(r"\[[^\[\]]*缺图\]", text):
            text = ""  # 只有缺图占位的段落：块里没有它的字
        norms.append(text)
    return norms


Occurrence = int | Callable[[], int] | None


def _pick(matches: list[list[int]], occurrence: Occurrence) -> list[int] | None:
    if len(matches) == 1:
        return matches[0]
    if matches and occurrence is not None:
        # 同一段话在页面上出现几遍：按这块在同文字的块里排第几挑（按页面顺序）。
        nth = occurrence() if callable(occurrence) else occurrence
        if 0 <= nth < len(matches):
            return sorted(matches, key=lambda covered: covered[0])[nth]
    return None


def _locate(htmls: list[str], norms: list[str], chunk_text: str, occurrence: Occurrence) -> list[int] | None:
    target = _norm(chunk_text)
    if len(target) < 8:
        return None
    exact = _exact(htmls, norms, target)
    if exact:
        return _pick(exact, occurrence)
    cleaned = _NOISE.sub("", target)
    return _pick(_fuzzy(norms, cleaned), occurrence) if len(cleaned) >= _KEY else None


def mark_quoted_parts(
    parts: list[str], targets: list[tuple[str, Occurrence]],
) -> tuple[list[str], bool]:
    """parts 是按页面顺序的几段 HTML（对照页每对的一侧），当成一整页找；targets 是要标的几块
    （块的字，它是同文字的第几处）。返回 (标好的各段, 有没有标上任何一块)。

    每块先找一字不差的，找不到再容小差异对齐。同一段话页面上有几处时，用「第几处」挑；不给就放弃这一块，
    页面顶上照旧放引用框。页面只拆一遍，几块（一个片段）一起标。
    """
    blocks: list[tuple[int, str]] = []
    for part, html in enumerate(parts):
        blocks.extend((part, block) for block in top_level_blocks(html))
    if not blocks:
        return parts, False
    norms = _norms(blocks)
    htmls = [block for _part, block in blocks]
    marked: set[int] = set()
    for chunk_text, occurrence in targets:
        chosen = _locate(htmls, norms, chunk_text, occurrence)
        if chosen:
            marked.update(chosen)
    if not marked:
        return parts, False
    touched = {blocks[index][0] for index in marked}
    out: list[list[str]] = [[] for _ in parts]
    for index, (part, block) in enumerate(blocks):
        out[part].append(_add_class(block) if index in marked else block)
    # 没标到的那几段原样留着，不经 top_level_blocks 重拼。
    return ["".join(out[part]) if part in touched else html for part, html in enumerate(parts)], True


def mark_quoted(html: str, chunk_text: str, *, occurrence: Occurrence = None) -> tuple[str, bool]:
    """返回 (标好的 HTML, 找到没有)。"""
    parts, found = mark_quoted_parts([html], [(chunk_text, occurrence)])
    return (parts[0], True) if found else (html, False)


def occurrence_of(chunks: list[str], index: int) -> int:
    """第 index 块的字在它前面的块里整段出现过几次：页面上同一段话有几处时，用来挑是第几处。"""
    target = _norm(chunks[index])
    return sum(1 for text in chunks[:index] if target and target in _norm(text))

"""Stratechery 每日更新照邮件导出，开头结尾那几句固定的话：不进检索，单看原文时也不显示。

每日更新（Daily Update，2023 年起叫 Stratechery Update，访谈是 Daily Update Interview）一篇大致是::

    **Listen to this post:**           （2026 年的网页导出是「Listen to this Update in your podcast player」）

    Good morning,

    （有时有作者写给读者的话：更正、回应读者、播客预告。这些留着）

    On to the Update:                  （访谈是「On to the Interview:」）

    ……正文……

    This Update will be available as a podcast later today. To receive it in your podcast player, visit Stratechery.

    The Stratechery Update is intended for a single recipient, but occasional forwarding is totally fine!
    If you would like to order multiple subscriptions for your team with a group discount (minimum 5),
    please contact me directly.

    Thanks for being a subscriber, and have a great day!

    Add to your podcast player: Stratechery | Sharp Tech | Dithering | Sharp China | GOAT | Asianometry

2014、2015 年的结尾是「Thanks for being a supporter. If you enjoy these Daily Updates … send a friend here
to sign up as well! In addition, if you would like to order multiple copies for your team, please contact me
directly.」和「Have a great day.」；2018 年底还有一句「Stratechery gift subscriptions are perfect for Christmas」。

线上检索「have a great day」命中约 1800 篇、「On to the Update」约 1500 篇。搜 subscriptions 命中 1715 篇，
前 20 条里 5 条的片段就是转发须知；问库也会把它当成一段原文。

只去整段都是这些句子的段落：「Listen…」「Good morning,」只看开头四段，「On to the Update:」和播客提醒
只看开头十五段；结尾从最后一段往前，碰到别的话就停。「Good morning,」和「On to the Update:」之间
作者写的话留着。

「段」按空行切，另外两种导出要细分：

- 邮件导出的标题行下面不空行，直接跟着「**Listen to this post:**」。以前一整块当标题跳过，问候和
  「On to the Interview:」去掉了，「Listen to this post:」留在正文第一行（线上 21343、21625）。
  标题行单独算一段。
- 2026 年的网页导出整篇一行一段，中间没有空行，链接还被拆成几行。开头的「Listen…」「Good morning,」
  「On to the Update:」单独占一行时单独算一段；结尾那一块从最后一行往前，找最长的、整段都是套话的
  几行（线上 22178 开头结尾一句都没去掉）。

用在检索文字和段落块（boilerplate.search_text、chunks.make_chunks）、单看原文的文章页和
/api/v1/articles/{id}.md。中英对照和只看中文照旧：译文是照着整篇译的，对照按位置配，原文这边少几段
就和译文错开。译制也照旧整篇送。库里的 body_md 不改。
"""

from __future__ import annotations

import html as html_lib
import re

from researchlib.nested_copies import markdown_blocks
from researchlib.textutil import markdown_to_text

# 先粗筛：一句都沾不上就原样返回，中文正文不用切块。
_ANY = re.compile(
    r"good morning|on to the|listen to this|have a (?:great|good|wonderful)|single recipient|"
    r"available as a podcast|podcast player|thanks for being a|gift subscriptions",
    re.I,
)
_FRONTMATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.S)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]|$)")
_RULE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# 链接被拆成几行导出时（2026 年的网页导出），标点前会多一个空格：「visit Stratechery .」。
_SPACE_BEFORE_MARK = re.compile(r" (?=[,.:;!?])")
# 看不见的格式字符（零宽空格、零宽连字、BOM、软连字符）。邮件和网页导出里偶尔夹着，比对前去掉。
_INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")


def _patterns(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.I) for pattern in patterns)


_OPENERS = _patterns(
    r"listen to this (?:post|update|interview|episode|article)(?: in your podcast player)?:?",
    r"good morning[,.!]?",
)
_HEAD = _patterns(
    r"on to the (?:daily |stratechery )?(?:update|interview)[:.!]?",
    r"as a reminder, all stratechery content(?:, including interviews,)? is available as a podcast;? "
    r"click the link at the top of this email to add stratechery to your podcast player[.!]?",
)
# 结尾的句子。一段里每一句都是这些才去掉。
_SIGNOFF = _patterns(
    r"(?:the|this) (?:daily |stratechery )?update(?: interview)? is intended for a single recipient, "
    r"but occasional forwarding is totally fine[.!]?",
    r"(?:in addition, )?if you would like to order multiple (?:subscriptions|copies) for your team"
    r"(?: with a group discount \(minimum \d+\))?, please contact me directly[.!]?",
    r"thanks for being a (?:supporter|subscriber|member)"
    r"(?:,? and have a (?:great|good|wonderful) (?:day|weekend|week))?[.!]?",
    r"have a (?:great|good|wonderful) (?:day|weekend|week)[.!]?",
    r"if you enjoy these daily updates and find them useful, please send a friend here to sign up as well[.!]?",
    r"this (?:daily )?update(?: interview)? (?:will be|is also|is) available as a podcast(?: later today)?[.!]?",
    r"to receive it in your podcast player, visit stratechery[.!]?",
    r"stratechery gift subscriptions are perfect for (?:christmas|the holidays)(?: and are available here)?[.!]?",
    # 2026 年网页导出页脚的播客列表：「Add to your podcast player: Stratechery | Sharp Tech | …」。
    r"add to your podcast player:?(?: [^|｜.!?]{1,40}(?: [|｜] [^|｜.!?]{1,40})*)?",
)
OPENER_BLOCKS = 4
HEAD_BLOCKS = 15
# 一块的结尾最多往前看这么多行找套话。2026 年网页导出的页脚连播客列表约 20 行。
TAIL_LINES = 60


def _text(lines: list[str]) -> str:
    text = _INVISIBLE.sub("", html_lib.unescape(markdown_to_text("\n".join(lines))))
    return _SPACE_BEFORE_MARK.sub("", re.sub(r"\s+", " ", text)).strip()


def _any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.fullmatch(text) for pattern in patterns)


def is_signoff(text: str) -> bool:
    """这一段（纯文字）是不是全由结尾套话组成。"""
    if not text:
        return False
    if _any(text, _SIGNOFF):
        return True
    sentences = [part for part in _SENTENCE.split(text) if part]
    return bool(sentences) and all(_any(sentence, _SIGNOFF) for sentence in sentences)


def _is_rule(lines: list[str]) -> bool:
    return all(_RULE.match(line) for line in lines)


def _opening_line(line: str) -> bool:
    """这一行单独就是一句开头的套话：「Listen…」「Good morning,」「On to the Update:」。"""
    if not _ANY.search(line):
        return False
    text = _text([line])
    return _any(text, _OPENERS) or _any(text, _HEAD)


def _units(lines: list[str]) -> list[tuple[int, int, bool]]:
    """按空行切的块，再把标题行和单独占一行的开头套话切出来，各算一段：[(起, 止, 含不含围栏代码)]，止不含。"""
    units: list[tuple[int, int, bool]] = []
    for start, end, code in markdown_blocks(lines):
        if code:
            units.append((start, end, code))
            continue
        piece = start
        for number in range(start, end):
            if _HEADING.match(lines[number]) or _opening_line(lines[number]):
                if piece < number:
                    units.append((piece, number, False))
                units.append((number, number + 1, False))
                piece = number + 1
        if piece < end:
            units.append((piece, end, False))
    return units


def _signoff_tail(lines: list[str], start: int, end: int) -> int | None:
    """这一段最后几行整截都是结尾套话时，返回最长那一截从哪一行起；没有就是 None。

    2026 年的网页导出整篇一行一段，套话和正文在同一段里，链接还被拆成几行（「visit Stratechery」「.」），
    所以往前一行一行接着试，不在第一行对不上时就停。整段都是套话的情况调用方已经看过。
    """
    first = max(start + 1, end - TAIL_LINES)
    if not _ANY.search("\n".join(lines[first:end])):
        return None
    found = None
    for number in range(end - 1, first - 1, -1):
        if is_signoff(_text(lines[number:end])):
            found = number
    return found


def strip_housekeeping(md: str) -> str:
    """去掉开头的「Listen…」「Good morning,」「On to the Update:」和结尾的套话。没有就原样返回。"""
    if not md or not _ANY.search(md):
        return md or ""
    front = _FRONTMATTER.match(md)
    head, body = (md[: front.end()], md[front.end() :]) if front else ("", md)
    lines = body.split("\n")
    units = _units(lines)
    gone: set[int] = set()
    seen = 0
    for start, end, code in units:
        if seen >= HEAD_BLOCKS:
            break
        chunk = lines[start:end]
        if not code and (_HEADING.match(chunk[0]) or _is_rule(chunk)):
            continue
        if not code:
            text = _text(chunk)
            if (seen < OPENER_BLOCKS and _any(text, _OPENERS)) or _any(text, _HEAD):
                gone.update(range(start, end))
        seen += 1
    keep = len(units)  # units[keep:] 是结尾的套话和夹在中间的分隔线
    for index in range(len(units) - 1, -1, -1):
        start, end, code = units[index]
        if code or start in gone:
            break
        chunk = lines[start:end]
        if _is_rule(chunk):
            continue
        if is_signoff(_text(chunk)):
            keep = index
            continue
        cut = _signoff_tail(lines, start, end)
        if cut is not None:
            gone.update(range(cut, end))
            keep = index + 1
        break
    if keep < len(units):
        # 套话和后面的分隔线都去掉，套话前面隔开正文的分隔线也去掉。
        while keep > 0 and not units[keep - 1][2] and _is_rule(lines[units[keep - 1][0] : units[keep - 1][1]]):
            keep -= 1
        for start, end, _code in units[keep:]:
            gone.update(range(start, end))
    if not gone:
        return md
    kept = "\n".join(line for number, line in enumerate(lines) if number not in gone)
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip("\n")
    return head + kept + ("\n" if body.endswith("\n") and kept else "")

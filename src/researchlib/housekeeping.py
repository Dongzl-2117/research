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
    r"add to your podcast player:?(?: [^|.!?]{1,40}(?: \| [^|.!?]{1,40})*)?",
)
OPENER_BLOCKS = 4
HEAD_BLOCKS = 15


def _text(lines: list[str]) -> str:
    text = html_lib.unescape(markdown_to_text("\n".join(lines)))
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


def strip_housekeeping(md: str) -> str:
    """去掉开头的「Listen…」「Good morning,」「On to the Update:」和结尾的套话段落。没有就原样返回。"""
    if not md or not _ANY.search(md):
        return md or ""
    front = _FRONTMATTER.match(md)
    head, body = (md[: front.end()], md[front.end() :]) if front else ("", md)
    lines = body.split("\n")
    blocks = markdown_blocks(lines)
    drop: set[int] = set()
    seen = 0
    for index, (start, end, code) in enumerate(blocks):
        if seen >= HEAD_BLOCKS:
            break
        chunk = lines[start:end]
        if not code and (_HEADING.match(chunk[0]) or _is_rule(chunk)):
            continue
        if not code:
            text = _text(chunk)
            if (seen < OPENER_BLOCKS and _any(text, _OPENERS)) or _any(text, _HEAD):
                drop.add(index)
        seen += 1
    first_signoff = None
    for index in range(len(blocks) - 1, -1, -1):
        start, end, code = blocks[index]
        chunk = lines[start:end]
        if code or index in drop:
            break
        if _is_rule(chunk):
            continue
        if not is_signoff(_text(chunk)):
            break
        first_signoff = index
    if first_signoff is not None:
        # 套话和后面的分隔线都去掉，套话前面隔开正文的分隔线也去掉。
        drop.update(range(first_signoff, len(blocks)))
        before = first_signoff - 1
        while before >= 0 and not blocks[before][2] and _is_rule(lines[blocks[before][0] : blocks[before][1]]):
            drop.add(before)
            before -= 1
    if not drop:
        return md
    gone = {line for index in drop for line in range(blocks[index][0], blocks[index][1])}
    kept = "\n".join(line for number, line in enumerate(lines) if number not in gone)
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip("\n")
    return head + kept + ("\n" if body.endswith("\n") and kept else "")

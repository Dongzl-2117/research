"""Stratechery 2026 年网页导出（归档头 ``- source: member-rss``）单看原文时拼回段落。

这种导出一个文本节点一行、整篇不空行：链接字单独一行，后面的「, mostly …」「. After that …」
另起一行，小标题也只是单独一行。阅读页一行一个换行，一句话被链接拆成三四行。

只给单看原文的文章页和文章接口（``/api/v1/articles/{id}``、``.md``、MCP 的 ``get_article``）用。
对照页、只看中文和译制读的是 ``canonical_body_html``，那里的段是旧译文按位置配对用的，
拼了就和已有译文对不上（见 docs/bilingual-mapping-2026-11-02.md）。

每一行只看它和已经拼好的上一段：

- 以句读、右括号、右引号开头的，直接贴上去，不加空格（「before」「, mostly …」）。
- 上一段没收句（最后是 . ! ? : …，后面可以再跟右引号、右括号，才算收句），这一行小写开头：空一格接上。
  访谈稿里补的人名「Jane [Doe]」后面常是小写的续句。
- 上一段以逗号、分号、破折号收尾，或者这一行以「(」开头：空一格接上。
- 上一段停在 the、of、in、that 这类虚词、told、called 这类动词或者所有格（「2023’s」）上，
  或者已经是半句长话（60 字以上、中间有逗号句号），这一行大写开头（链接字常是文章名、人名、刊名）：也接上。
- 访谈的说话人「Jane Doe:」「JD:」（一两个大写开头的词加冒号）和后面那句接成一段。
- 单独一行的「|」是栏目列表的分隔，前后接成一行。

其余各成一段，所以小标题单独一段。

Markdown 的块照它原来的样子显示：标题、图片单独一段；引用、HTML 和空行后面的列表、表格原样留到下一个空行；
代码围栏整块不动。跟在正文行后面的「2026. After that」「- 」「| a | b |」网页上本来就是那一段里的一行
（Markdown 不让它们打断段落），接不上的时候照旧换行留在那一段里，不另起一段，免得变成编号列表、吞掉年份。
"""

from __future__ import annotations

import re

_MEMBER_RSS = re.compile(r"(?m)^[ \t]*-[ \t]*source:[ \t]*member-rss[ \t]*$")
_LEADS_ON = re.compile(r"^[,.;:!?)\]}”’…]")
# 收句：句读后面可以再跟右引号、右括号；访谈每节末尾紧贴句号的空锚点「[]()」不算字。
_CLOSED = re.compile(r"[.!?:…][\"”’)\]]*(?:\[\]\([^)]*\))*$")
_LAST_WORD = re.compile(r"(\w+)([’']s)?$")
_SPEAKER = re.compile(r"^(?!From |Via |Per )[A-Z][\w.’'-]*(?: [A-Z][\w.’'-]*)?:$")
_INNER_PUNCT = re.compile(r"[,;] (?!\d)|(?<!\bvs)(?<!\bMr)(?<!\bMs)(?<!\bDr)(?<!\bSt)[.!?] [A-Z“\"]")
# 小标题一般不到这么长；没收句、又比这长的是被链接切开的半句。
_HEADING_MAX = 60
# 单独成段的：标题、图片。
_STANDALONE = re.compile(r"^(?:#{1,6}\s|!\[)")
# 在哪儿都会自成一块、一直到空行的：引用（后面的行算引用里的）、HTML 块。
_OPENS_BLOCK = re.compile(r"^(?:>|<[A-Za-z/!])")
# 空行（或标题）后面才算块的：列表、表格。
_AFTER_BLANK = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|\|.*\|)")
_FENCE = re.compile(r"^(?:```|~~~)")
_FUNCTION_WORDS = frozenset(
    """
    a an the and or but nor of in on at by for from to with about as than that which who whom whose
    via into onto over under before after like per is are was were be been this these those
    my our your their his her its
    told said says wrote writes called named titled dubbed described noted argued explained asked
    """.split()
)


def is_web_export(body_md: str) -> bool:
    """归档头里写着 ``- source: member-rss`` 的是 2026 年网页导出。"""
    return bool(_MEMBER_RSS.search((body_md or "")[:2000]))


def _continues(previous: str, line: str) -> str | None:
    """这一行接在上一段后面时，返回中间要补的字（空串或一个空格）；另起一段返回 None。"""
    if previous.endswith(" |"):
        return " "
    if _SPEAKER.match(line):
        return None
    if _LEADS_ON.match(line):
        return ""
    if previous.endswith(("(", "“", "‘", "[")):
        return ""
    if _SPEAKER.match(previous):
        # 访谈的「Jane Doe:」「JD:」、栏目的「Topics:」：和后面那句同一段。
        return " "
    if _CLOSED.search(previous):
        return None
    if previous.endswith((",", ";", "—", "–")) or line[:1].islower() or line[:1] == "(":
        return " "
    if not (line[:1].isupper() or line[:1].isdigit() or line[:1] in "“\"‘'"):
        return None
    last = _LAST_WORD.search(previous)
    # 停在虚词、说/叫这类动词、或者所有格（「2023’s」）上，后面是链接字（文章名、人名、刊名）。
    if last and (last.group(2) or last.group(1).lower() in _FUNCTION_WORDS):
        return " "
    # 已经是半句长话（够长、或者中间有逗号句号），不是小标题：大写开头的链接字也接上。
    if len(previous) >= _HEADING_MAX or _INNER_PUNCT.search(previous):
        return " "
    return None


def join_web_export_lines(md: str) -> str:
    """一行一个文本节点的正文拼成段落，段与段之间空一行。是不是这种导出由调用方用 is_web_export 判断。"""
    blocks: list[str] = []
    joinable = False  # 上一块是正文段，这一行可以往上接
    verbatim = False  # 上一块是引用、HTML、列表、表格：到下一个空行为止原样留着
    piped = False  # 这一段是「A | B | C」栏目列表：最后一项后面不再往上接
    fence: list[str] | None = None
    for raw in (md or "").split("\n"):
        line = raw.strip()
        if fence is not None:
            fence.append(raw)
            if _FENCE.match(line):
                blocks.append("\n".join(fence))
                fence = None
            continue
        if _FENCE.match(line):
            fence = [raw]
            joinable = verbatim = False
            continue
        if not line:
            joinable = verbatim = False
            continue
        if verbatim:
            blocks[-1] += "\n" + raw.rstrip()
            continue
        if _STANDALONE.match(line):
            blocks.append(line)
            joinable = False
            continue
        if _OPENS_BLOCK.match(line) or (not joinable and _AFTER_BLANK.match(line)):
            blocks.append(raw.rstrip())
            joinable, verbatim = False, True
            continue
        if line == "|":
            if joinable and blocks:
                blocks[-1] = blocks[-1].rstrip() + " |"
                piped = True
            continue
        if joinable and blocks:
            previous = blocks[-1].rstrip()
            glue = None
            if piped and not previous.endswith(" |"):
                piped = False
            else:
                glue = _continues(previous, line)
            if glue is None and _AFTER_BLANK.match(line):
                # 另起一段就成了列表、表格：网页上本来就是这一段里换行的一行，照旧。
                glue = "\n"
            if glue is not None:
                blocks[-1] = previous + glue + line
                continue
        blocks.append(line)
        joinable = True
        piped = False
    if fence is not None:
        blocks.append("\n".join(fence))
    return "\n\n".join(blocks) + ("\n" if (md or "").endswith("\n") and blocks else "")

"""知识星球导出的「评论（N）」列表。

归档正文里这一节是 Markdown：`- **名字**（时间）` 再加缩进的正文。
名字被打成空白（常见是全角空格）时，`**　　**` 做不成强调，星号会原样留在页面上。
阅读页改成作者、时间、正文；空白名字写成「匿名」。标题前面那些空 `<br>` 去掉。
阅读页、入库和接口都只留这个来源的作者自己的评论（方伟星球留方伟，乘风破浪留星主），其他人的可逆隐藏；
inbox / raw 里的原文不动。作者认来源配置的 author 和 poster_names（`source_owners`），不给时按方伟认。
楼中楼的回复导出成「乘风破浪 回复 王小明」：看「回复」前面那个人。星主回复读者的留下，写成「乘风破浪 回复网友」，
不留读者的昵称；读者回复星主的照样不留。
「评论里的回复」是另一节；阅读页把旧标签映射成「网友：」「方伟：」，与正文同一套对话体。
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace

import markdown

from researchlib.textutil import escape_times

ANONYMOUS = "匿名"

_HEADING = re.compile(r"^##\s*评论\s*[（(]\s*\d+\s*[)）]\s*$", re.M)
_NEXT_HEADING = re.compile(r"^#{1,6}\s+\S", re.M)
_TIME = r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}[:：]\d{2}(?:[:：]\d{2})?"
_MARKER = r"(?:[-*+]|[0-9]{1,3}[.)])"
_ITEM = re.compile(
    rf"^{_MARKER}[ \t]+(?P<name>.*?)[ \t]*[（(](?P<time>{_TIME})[)）][ \t]*(?P<rest>.*)$"
)
_LOOSE_ITEM = re.compile(rf"^{_MARKER}[ \t]+\S")
_BLANK = re.compile(r"^[ \t]*(?:<br\s*/?>[ \t]*)*$", re.I)
_EMPH_WRAP = re.compile(r"^(?P<mark>\*\*|__|~~|\*|_)(?P<inner>[\s\S]*?)(?P=mark)$")
_CLOCK = re.compile(
    r"(?P<y>\d{4})[-/](?P<mo>\d{1,2})[-/](?P<d>\d{1,2})"
    r"[ T](?P<h>\d{1,2})[:：](?P<mi>\d{2})(?:[:：](?P<s>\d{2}))?"
)
_BR = re.compile(r"<br\s*/?>", re.I)
_LETTERS = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_FENCE = re.compile(r"^[ \t]{0,3}```")
_EMPTY_P = re.compile(r"<p\b[^>]*>\s*(?:<br\s*/?>\s*)*</p>\s*$", re.I)
_BR_END = re.compile(r"<br\s*/?>\s*$", re.I)
_TRAILING_BR_IN_P = re.compile(r"(?:<br\s*/?>\s*)+(</p>\s*)$", re.I)
_SECTION_OPEN = re.compile(r"<section\b[^>]*\bclass=\"zsxq-comments\"", re.I)
# 楼中楼：「乘风破浪 回复 王小明」，回复的人在前。名字里的空格（「李 四 回复 乘风破浪」）留给前一半。
_REPLY = re.compile(r"^(?P<who>\S.*?)\s+回复\s*(?P<to>.*)$")
REPLY_TO_GUEST = "回复网友"


@dataclass(frozen=True)
class ReaderComment:
    author: str
    when: str
    stamp: str
    body_html: str


def display_author(raw: str) -> str:
    """空白、只有星号、或全是不可见字符的名字，显示成「匿名」。"""
    text = html.unescape(raw or "")
    text = _BR.sub("", text).strip()
    for _ in range(5):
        match = _EMPH_WRAP.match(text)
        if not match:
            break
        inner = match.group("inner").strip()
        if inner == text:
            break
        text = inner
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cf", "Cc"})
    text = re.sub(r"\s+", " ", text).strip().strip("*_~`")
    if not text or not _LETTERS.search(text):
        return ANONYMOUS
    return text


def source_owners(source) -> tuple[str, ...]:
    """来源作者在评论里用的名字：author 加 poster_names（星球昵称）。"""
    return tuple(name for name in (source.author, *source.poster_names) if name)


def _is_owner(shown: str, owners: Sequence[str]) -> bool:
    from researchlib.author_voice import same_person
    from researchlib.wechat_dialogue import _compact_host, _is_host_line

    if not shown or shown == ANONYMOUS:
        return False
    # 方伟的显示名花样多（方伟Rey、方伟-最近在杭州），沿用对话整理那一套。
    if (not owners or "方伟" in owners) and _is_host_line(_compact_host(shown)):
        return True
    return any(same_person(shown, owner) for owner in owners)


def _host_comment(shown: str, owners: Sequence[str]) -> tuple[str, bool] | None:
    """shown 是 display_author 过的名字。来源作者自己的评论返回 (页面上的名字, 是不是楼中楼的回复)，
    别人的返回 None。回复读者的名字换成「乘风破浪 回复网友」，回复他自己的照原样。
    """
    reply = _REPLY.match(shown)
    if reply is None:
        return (shown, False) if _is_owner(shown, owners) else None
    who = reply.group("who").strip()
    if not _is_owner(who, owners):
        return None
    if _is_owner(display_author(reply.group("to")), owners):
        return shown, True
    return f"{who} {REPLY_TO_GUEST}", True


def is_host_comment_author(name: str, owners: Sequence[str] = ()) -> bool:
    """是不是来源作者自己的评论（星主回复别人的也算）。owners 见 source_owners，不给时按方伟认。空白「匿名」不算。"""
    return _host_comment(display_author(name), owners) is not None


def keep_host_comments(comments: list[ReaderComment], owners: Sequence[str] = ()) -> list[ReaderComment]:
    kept: list[ReaderComment] = []
    for comment in comments:
        host = _host_comment(comment.author, owners)
        if host is not None:
            kept.append(replace(comment, author=host[0]))
    return kept


def _parse_comment_items(section: str) -> list[tuple[str, str, list[str]]] | None:
    """拆出 (名字, 时间, 正文行)。格式不对返回 None。"""
    lines = (section or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    items: list[tuple[str, str, list[str]]] = []
    current: tuple[str, str, list[str]] | None = None

    def start(match: re.Match[str]) -> tuple[str, str, list[str]]:
        rest = _BR.sub("\n", match.group("rest") or "")
        body = [line.strip() for line in rest.split("\n") if line.strip()]
        return (match.group("name"), match.group("time"), body)

    for line in lines:
        if _BLANK.match(line):
            if current is not None and current[2] and current[2][-1] != "":
                current[2].append("")
            continue
        match = _ITEM.match(line)
        if match is None and line[:1] in " \t":
            match = _ITEM.match(line.strip())
        if match:
            if current is not None:
                items.append(current)
            current = start(match)
            continue
        if _LOOSE_ITEM.match(line) or current is None:
            return None
        current[2].append(line.strip())
    if current is not None:
        items.append(current)
    if not items:
        return None
    return items


def comment_list_authors(md: str) -> list[str] | None:
    """「## 评论（N）」里的显示名。没有这一节返回 []。标题在、但不是这种列表时返回 None。"""
    match = _HEADING.search(md or "")
    while match and _in_fence(md, match.start()):
        match = _HEADING.search(md, match.end())
    if not match or (match.start() > 0 and md[match.start() - 1] != "\n"):
        return []
    rest = md[match.end() :]
    nxt = _NEXT_HEADING.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    items = _parse_comment_items(section)
    if items is None:
        return None
    return [display_author(name) for name, _when, _body in items]


def guest_comment_authors(md: str, owners: Sequence[str] = ()) -> list[str] | None:
    """评论区里露出来的别人的名字（owners 不给时按方伟）：不是来源作者的评论人，加上星主回复的那位
    （「乘风破浪 回复 王小明」里的王小明）。没有评论节返回 []。解析不了返回 None。"""
    names = comment_list_authors(md)
    if names is None:
        return None
    guests: list[str] = []
    for name in names:
        host = _host_comment(name, owners)
        if host is None:
            guests.append(name)
        elif host[0] != name:
            # 星主回复读者，名字里还带着那位读者。
            guests.append(display_author(_REPLY.match(name).group("to")))
    return guests


# 检索文字（body_text）里的评论节：「评论（2）」一行，每条是「名字（2026-02-02 21:57）」再跟正文，
# 列表横线和加粗都没了。Markdown 原文里是「## 评论（2）」「- **名字**（时间）」，两种都认。
_TEXT_HEADING = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?评论[ \t]*[（(][ \t]*\d+[ \t]*[)）][ \t]*$")
_TEXT_ITEM = re.compile(rf"^[ \t]*(?:{_MARKER}[ \t]+)?(?P<name>.{{0,40}}?)[ \t]*[（(](?P<time>{_TIME})[)）]")
_TEXT_END = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+\S|评论里的回复[ \t]*$)")


def drop_guest_comments_text(text: str, owners: Sequence[str] = ()) -> str:
    """正文或检索文字里「评论（N）」那一节只留来源作者自己的评论（owners 不给时按方伟），给标题挑句子用。

    只有图的帖以前拿读者的评论起了题：线上 23834 的标题是一位读者的网名、评论时间和那句评论，
    文章页的评论区早就不显示这条。一条都不剩就连「评论（N）」一起去掉；认不出评论头的节原样留着。
    """
    if not text or "评论" not in text:
        return text or ""
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        if not _TEXT_HEADING.match(lines[index]):
            out.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not _TEXT_END.match(lines[end]) and not _TEXT_HEADING.match(lines[end]):
            end += 1
        section = lines[index + 1 : end]
        if not any(_TEXT_ITEM.match(line) for line in section):
            out.extend(lines[index:end])
            index = end
            continue
        kept: list[str] = []
        keep = False
        for line in section:
            match = _TEXT_ITEM.match(line)
            if match:
                keep = _host_comment(display_author(match.group("name")), owners) is not None
            if keep:
                kept.append(line)
        if kept:
            out.append(lines[index])
            out.extend(kept)
        index = end
    return "\n".join(out)


def filter_host_comments_markdown(md: str, owners: Sequence[str] = (), *, replies: bool = True) -> str:
    """正文里的「评论（N）」只留来源作者（owners 不给时按方伟）。一条都不剩就整节去掉。inbox 原文不改。

    replies=False 时星主在楼中楼里的回复也不留：导入时话题 JSON 里有这些回复，会连同读者那句
    一起接进「评论里的回复」（parse.comments_section），比列表里只剩一句回答清楚。
    """
    match = _HEADING.search(md or "")
    while match and _in_fence(md, match.start()):
        match = _HEADING.search(md, match.end())
    if not match or (match.start() > 0 and md[match.start() - 1] != "\n"):
        return md or ""
    rest = md[match.end() :]
    nxt = _NEXT_HEADING.search(rest)
    if nxt:
        section, after = rest[: nxt.start()], rest[nxt.start() :]
    else:
        section, after = rest, ""
    items = _parse_comment_items(section)
    if items is None:
        return md or ""
    kept: list[tuple[str, str, list[str]]] = []
    for name, when, body in items:
        host = _host_comment(display_author(name), owners)
        if host is not None and (replies or not host[1]):
            kept.append((host[0], when, body))
    before = md[: match.start()]
    if not kept:
        glued = before.rstrip() + ("\n\n" + after.lstrip("\n") if after.strip() else "\n")
        return glued if glued.endswith("\n") else glued + "\n"
    out = [f"## 评论（{len(kept)}）", ""]
    for label, when, body in kept:
        out.append(f"- **{label}**（{when}）")
        if any(line.strip() for line in body):
            for line in body:
                out.append(f"  {line}" if line.strip() else "")
        out.append("")
    rebuilt = "\n".join(out).rstrip() + "\n"
    if after.strip():
        return before.rstrip() + "\n\n" + rebuilt + "\n" + after.lstrip("\n")
    return before.rstrip() + "\n\n" + rebuilt


def _format_when(raw: str) -> tuple[str, str]:
    match = _CLOCK.search(raw or "")
    if not match:
        text = (raw or "").strip()
        return text, ""
    year, month, day = (int(match.group("y")), int(match.group("mo")), int(match.group("d")))
    hour, minute = int(match.group("h")), match.group("mi")
    shown = f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute}"
    stamp = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute}"
    second = match.group("s")
    if second and second != "00":
        shown += f":{second}"
        stamp += f":{second}"
    return shown, stamp


def _flow(text: str) -> str:
    """同一段里的换行接上。中文之间不塞空格，英文单词之间留一个。"""
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    flowed: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        buf = lines[0]
        for line in lines[1:]:
            # 两边都是英文才空一格。汉字、全角标点直接接上。
            gap = " " if buf and buf[-1].isascii() and line[0].isascii() else ""
            buf += gap + line
        flowed.append(buf)
    return "\n\n".join(flowed)


def _comment_body_html(text: str, mapping: dict[str, str]) -> str:
    from researchlib.render import _anchor_images, rewrite_html_images, rewrite_markdown_images, sanitize_html

    flowed = _flow(_BR.sub("\n", text or ""))
    if not flowed.strip():
        return ""
    rewritten = rewrite_html_images(rewrite_markdown_images(escape_times(flowed), mapping), mapping)
    rendered = markdown.markdown(rewritten, extensions=["extra", "sane_lists"], output_format="html")
    return _anchor_images(sanitize_html(rendered))


def _to_comment(name: str, when: str, body: list[str], mapping: dict[str, str]) -> ReaderComment:
    shown, stamp = _format_when(when)
    return ReaderComment(
        author=display_author(name),
        when=shown,
        stamp=stamp,
        body_html=_comment_body_html("\n".join(body), mapping),
    )


def parse_reader_comments(section: str, mapping: dict[str, str] | None = None) -> list[ReaderComment] | None:
    """是「- **名字**（时间）」这种列表就返回评论，否则返回 None，调用方照旧渲染。"""
    mapping = mapping or {}
    items = _parse_comment_items(section)
    if items is None:
        return None
    return [_to_comment(name, when, body, mapping) for name, when, body in items]

def _in_fence(md: str, index: int) -> bool:
    fenced = False
    for line in md[:index].split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
    return fenced


def split_reader_comments(
    md: str, mapping: dict[str, str] | None = None
) -> tuple[str, list[ReaderComment] | None, str]:
    """(评论之前, 评论, 下一节及之后)。对不上这种列表时，评论是 None，原文不动。"""
    match = _HEADING.search(md or "")
    while match and _in_fence(md, match.start()):
        match = _HEADING.search(md, match.end())
    if not match or (match.start() > 0 and md[match.start() - 1] != "\n"):
        return md or "", None, ""
    rest = md[match.end() :]
    nxt = _NEXT_HEADING.search(rest)
    if nxt:
        section, after = rest[: nxt.start()], rest[nxt.start() :]
    else:
        section, after = rest, ""
    comments = parse_reader_comments(section, mapping)
    if comments is None:
        return md, None, ""
    return md[: match.start()], comments, after


def render_comment_section(comments: list[ReaderComment]) -> str:
    items: list[str] = []
    for comment in comments:
        author = html.escape(comment.author)
        when = html.escape(comment.when)
        stamp = html.escape(comment.stamp, quote=True)
        time = f'<time datetime="{stamp}">{when}</time>' if stamp else f"<time>{when}</time>"
        body = f'<div class="zsxq-comment-body">{comment.body_html}</div>' if comment.body_html else ""
        items.append(
            '<li class="zsxq-comment" role="listitem">'
            '<p class="zsxq-comment-meta">'
            f'<span class="zsxq-comment-author">{author}</span>'
            '<span class="zsxq-comment-dot" aria-hidden="true">·</span>'
            f"{time}</p>{body}</li>"
        )
    return (
        '<section class="zsxq-comments">'
        f"<h2>评论（{len(comments)}）</h2>"
        '<ol class="zsxq-comment-list" role="list">'
        + "".join(items)
        + "</ol></section>"
    )


def strip_trailing_breaks(html: str) -> str:
    """去掉结尾那些只含 <br> 的空段，以及段末尾多余的 <br>。"""
    head = html or ""
    while True:
        stripped = head.rstrip()
        if stripped != head:
            head = stripped
            continue
        match = _EMPTY_P.search(head)
        if match:
            head = head[: match.start()]
            continue
        match = _BR_END.search(head)
        if match:
            head = head[: match.start()]
            continue
        match = _TRAILING_BR_IN_P.search(head)
        if match:
            head = head[: match.start()] + match.group(1)
            continue
        break
    return head


def strip_breaks_before_comments(html: str) -> str:
    """评论节前面不要留一串空换行。图片被换成对话之后，段尾的 <br> 也算。"""
    match = _SECTION_OPEN.search(html or "")
    if not match:
        return html or ""
    return strip_trailing_breaks(html[: match.start()]) + html[match.start() :]


def assemble_reader_comments(body: str, mapping: dict[str, str], render_markdown, owners: Sequence[str] = ()) -> str:
    before, comments, after = split_reader_comments(body, mapping)
    if not comments:
        return render_markdown(body, mapping)
    comments = keep_host_comments(comments, owners)
    head = strip_trailing_breaks(render_markdown(before, mapping))
    tail = assemble_reader_comments(after, mapping, render_markdown, owners) if after.strip() else ""
    if not comments:
        return head + tail
    return head + render_comment_section(comments) + tail

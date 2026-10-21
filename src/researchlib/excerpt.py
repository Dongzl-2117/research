"""长文交给模型时取哪些段落。

以前摘要只给正文前 12000 字、观点只给前 30000 字：长文的结论和估值常在后半段，后半段才谈到的
公司会被记成「没下判断」。现在：

- 摘要：全文不超过上限（和问库一次读的量一样）就给全文；更长的取开头、结尾和每一节的第一块，
  剩下的名额从中间均匀补，按原文顺序拼起来，跳过的地方用省略号隔开。
- 观点：全文不超过上限就给全文；更长的只给提到这几家公司的段落块（和它后面一块），
  加上开头两块和最后一块。

段落块就是检索用的 article_chunks（原文的，lang = ''），一块几百字。
"""

from __future__ import annotations

import re

DIGEST_LIMIT = 64000
VIEWS_LIMIT = 30000
GAP = "\n\n……（中间略）……\n\n"
_LATIN = re.compile(r"^[A-Za-z0-9 .&+\-]+$")


def original_chunks(conn, article_id: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT heading, body FROM article_chunks WHERE article_id = %s AND lang = '' ORDER BY ord",
        (article_id,),
    ).fetchall()
    return [(row["heading"] or "", row["body"] or "") for row in rows]


def _render(chunks: list[tuple[str, str]], picked: set[int]) -> str:
    parts: list[str] = []
    previous = -2
    last_heading = None
    for index in sorted(picked):
        heading, body = chunks[index]
        if parts and index != previous + 1:
            parts.append(GAP)
        elif parts:
            parts.append("\n\n")
        if heading and heading != last_heading:
            parts.append(f"## {heading}\n")
        last_heading = heading
        parts.append(body)
        previous = index
    return "".join(parts)


def _size(chunks: list[tuple[str, str]], index: int) -> int:
    heading, body = chunks[index]
    return len(body) + len(heading) + 8


def digest_input(body_text: str, chunks: list[tuple[str, str]], limit: int = DIGEST_LIMIT) -> tuple[str, bool]:
    """返回 (交给模型的正文, 是否删减过)。"""
    text = body_text or ""
    if len(text) <= limit or not chunks:
        return text[:limit], len(text) > limit
    picked: set[int] = set()
    used = 0

    def take(index: int, cap: int) -> bool:
        nonlocal used
        if index in picked:
            return True
        size = _size(chunks, index)
        if used + size > cap:
            return False
        picked.add(index)
        used += size
        return True

    count = len(chunks)
    # 开头占四成、结尾占两成。
    for index in range(count):
        if not take(index, int(limit * 0.4)):
            break
    tail_cap = used + int(limit * 0.2)
    for index in range(count - 1, -1, -1):
        if not take(index, tail_cap):
            break
    # 每一节的第一块：作者的小标题往往就是论点。
    for index in range(1, count):
        if chunks[index][0] and chunks[index][0] != chunks[index - 1][0]:
            take(index, limit)
    # 还有名额就从中间均匀补。
    rest = [index for index in range(count) if index not in picked]
    if rest:
        step = max(1, len(rest) // max(1, (limit - used) // 600 or 1))
        for index in rest[::step]:
            take(index, limit)
    return _render(chunks, picked), True


def _alias_pattern(aliases: list[str]) -> re.Pattern[str] | None:
    parts = []
    for alias in sorted({a for a in aliases if a and len(a) >= 2}, key=len, reverse=True):
        escaped = re.escape(alias)
        if _LATIN.match(alias):
            parts.append(r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])")
        else:
            parts.append(escaped)
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


def views_input(
    body_text: str, chunks: list[tuple[str, str]], aliases: list[str], limit: int = VIEWS_LIMIT
) -> tuple[str, bool]:
    """返回 (交给模型的正文, 是否删减过)。aliases 是这次要整理的几家公司的全部写法。"""
    text = body_text or ""
    if len(text) <= limit or not chunks:
        return text[:limit], len(text) > limit
    pattern = _alias_pattern(aliases)
    count = len(chunks)
    hits = [
        index for index in range(count) if pattern is not None and pattern.search(chunks[index][0] + chunks[index][1])
    ]
    order: list[int] = []
    for index in hits:
        order.append(index)
    for index in (0, 1, count - 1):
        if 0 <= index < count:
            order.append(index)
    for index in hits:
        if index + 1 < count:
            order.append(index + 1)
    picked: set[int] = set()
    used = 0
    for index in order:
        if index in picked:
            continue
        size = _size(chunks, index)
        if used + size > limit:
            continue
        picked.add(index)
        used += size
    return _render(chunks, picked), True


VIEW_WINDOW = 12000
MAX_WINDOWS = 6


def _hard_split(text: str, window: int, limit: int = MAX_WINDOWS) -> list[str]:
    parts: list[str] = []
    rest = text or ""
    while rest and len(parts) < limit:
        if len(rest) <= window:
            parts.append(rest)
            break
        cut = rest.rfind("\n", window // 2, window)
        if cut < window // 2:
            cut = window
        piece = rest[:cut].strip()
        if piece:
            parts.append(piece)
        rest = rest[cut:].strip()
    return parts


def view_windows(
    body_text: str,
    chunks: list[tuple[str, str]],
    aliases: list[str],
    window: int = VIEW_WINDOW,
) -> list[str]:
    """超长文档先切块。每一块不超过 window，优先留下提到这些公司的段落、开头和结尾。"""
    text = body_text or ""
    if not text.strip():
        return []
    if len(text) <= window or not chunks:
        if len(text) <= window:
            return [text]
        return _hard_split(text, window)
    pattern = _alias_pattern(aliases)
    count = len(chunks)
    hits = [
        index
        for index in range(count)
        if pattern is not None and pattern.search(chunks[index][0] + "\n" + chunks[index][1])
    ]
    order: list[int] = []
    seen: set[int] = set()

    def add(index: int) -> None:
        if 0 <= index < count and index not in seen:
            seen.add(index)
            order.append(index)

    for index in (0, 1, count - 1):
        add(index)
    for index in hits:
        add(index)
        add(index + 1)
    order.sort()
    if not order:
        return _hard_split(text, window)
    groups: list[list[int]] = []
    bucket: list[int] = []
    size = 0
    for index in order:
        piece = _size(chunks, index)
        if bucket and size + piece > window:
            groups.append(bucket)
            bucket = []
            size = 0
            if len(groups) >= MAX_WINDOWS:
                break
        bucket.append(index)
        size += piece
    if bucket and len(groups) < MAX_WINDOWS:
        groups.append(bucket)
    rendered = [_render(chunks, set(group)) for group in groups if group]
    return [part for part in rendered if part.strip()] or _hard_split(text, window)

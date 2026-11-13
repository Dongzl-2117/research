"""长文交给模型时取哪些段落。

以前摘要只给正文前 12000 字、观点只给前 30000 字：长文的结论和估值常在后半段，后半段才谈到的
公司会被记成「没下判断」。现在：

- 摘要：全文不超过上限（和问库一次读的量一样）就给全文；更长的取开头、结尾和每一节的第一块，
  剩下的名额从中间均匀补，按原文顺序拼起来，跳过的地方用省略号隔开。
- 观点：不超过 12000 字给全文；更长的只给提到这几家公司的段落块（和它后面一块），
  加上开头两块和最后一块，按原文顺序切成几块分别问。选中的段落都读到，不设块数上限（view_windows）。

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


VIEW_WINDOW = 12000
# 长文平常最多切几块问。选中的段落装不进这么多块时每块放大（最大 VIEWS_LIMIT），再装不下就多切几块，一段都不丢。
MAX_WINDOWS = 6


def _alias_hits(chunks: list[tuple[str, str]], aliases: list[str]) -> list[int]:
    pattern = _alias_pattern(aliases)
    if pattern is None:
        return []
    return [index for index, (heading, body) in enumerate(chunks) if pattern.search(heading + "\n" + body)]


def _view_order(chunks: list[tuple[str, str]], aliases: list[str]) -> list[int]:
    """观点要读的段落块：开头两块、最后一块，提到这些公司的块和它后面一块，按原文顺序。"""
    count = len(chunks)
    picked = {index for index in (0, 1, count - 1) if 0 <= index < count}
    for index in _alias_hits(chunks, aliases):
        picked.update(index for index in (index, index + 1) if index < count)
    return sorted(picked)


def _cost(chunks: list[tuple[str, str]], index: int) -> int:
    """这一块拼进去最多占多少字：正文、小标题行，加上前面的省略号。"""
    heading, body = chunks[index]
    return len(body) + len(heading) + len(GAP) + 4


def window_size(total: int, window: int = VIEW_WINDOW, limit: int = VIEWS_LIMIT) -> int:
    """要读 total 字时一块多大：平常 window；MAX_WINDOWS 块装不下就放大，最大 limit。"""
    return max(window, min(limit, -(-int(total) // MAX_WINDOWS)))


def _hard_split(text: str, window: int) -> list[str]:
    """按长度切，尽量切在换行处。整段都留下，不限块数。"""
    parts: list[str] = []
    rest = text or ""
    while rest:
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
    limit: int = VIEWS_LIMIT,
) -> list[str]:
    """观点要读的正文，切成一块块分别问。短文整篇一块。

    长文只读提到这些公司的段落（和它后面一块）加开头结尾，按原文顺序装块，跳过的地方用省略号隔开。
    选中的段落都会读到：装不进 MAX_WINDOWS 块就把每块放大到最多 limit 字，还装不下就多切几块；
    比一块还长的段落（整篇的图片文字常这样）按长度切开。以前最多 6 块 12000 字，多出来的连同结尾一起丢了。
    """
    text = body_text or ""
    if not text.strip():
        return []
    if len(text) <= window:
        return [text]
    if not chunks:
        return _hard_split(text, window_size(len(text), window, limit))
    order = _view_order(chunks, aliases)
    size = window_size(sum(_cost(chunks, index) for index in order), window, limit)
    # 比一块还长的段落切开，切出来的几段挨着放，拼的时候仍算连着的。
    pieces: list[tuple[str, str]] = []
    picked: list[int] = []
    wanted = set(order)
    for index, (heading, body) in enumerate(chunks):
        parts = [body]
        if _cost(chunks, index) > size:
            parts = _hard_split(body, max(window // 2, size - len(heading) - len(GAP) - 4)) or [body]
        for part in parts:
            if index in wanted:
                picked.append(len(pieces))
            pieces.append((heading, part))
    groups: list[list[int]] = []
    bucket: list[int] = []
    used = 0
    for index in picked:
        cost = _cost(pieces, index)
        if bucket and used + cost > size:
            groups.append(bucket)
            bucket, used = [], 0
        bucket.append(index)
        used += cost
    if bucket:
        groups.append(bucket)
    rendered = [_render(pieces, set(group)) for group in groups]
    return [part for part in rendered if part.strip()] or _hard_split(text, size)


def old_reader_dropped(body_text: str, chunks: list[tuple[str, str]], aliases: list[str]) -> bool:
    """11-14 以前的读法读这篇时有没有丢段落。只用来找出要重抽的旧判断（research-views --truncated）。

    那时选中的段落按 12000 字一块装，装满 6 块就停，后面的（常常是结尾）不读；一块里只有一段、
    超过 30000 字时（整篇的图片文字），这一段换成按公司重选的片段；没有段落块时按长度切，只留前 6 段。
    """
    text = body_text or ""
    if not text.strip() or len(text) <= VIEW_WINDOW:
        return False
    if not chunks:
        return len(_hard_split(text, VIEW_WINDOW)) > MAX_WINDOWS
    groups = 0
    used = 0
    for index in _view_order(chunks, aliases):
        if len(_render(chunks, {index})) > VIEWS_LIMIT:
            return True
        size = _size(chunks, index)
        if used and used + size > VIEW_WINDOW:
            groups += 1
            used = 0
            if groups >= MAX_WINDOWS:
                return True
        used += size
    return False

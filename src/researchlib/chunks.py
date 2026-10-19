"""把正文切成段落块。检索片段、JSON 接口和 LLM 问答的引用都落在块上。

块按 Markdown 章节切，章节内按段落装箱，目标几百字一块。块带上所属章节标题，
这样单看一块也知道它在谈什么。
"""

from __future__ import annotations

import re
import sys

from researchlib.textutil import markdown_to_text, split_frontmatter, split_markdown_sections, strip_duplicate_title

TARGET_CHARS = 500
MAX_CHARS = 800
MAX_CHUNKS = 400
_HEADING_LINE = re.compile(r"\A\s*#{1,6}\s+(\S.*?)\s*(?:\n|$)")
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])|\n")


def _split_long(paragraph: str) -> list[str]:
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    pieces: list[str] = []
    buf = ""
    for sentence in _SENTENCE_END.split(paragraph):
        if not sentence:
            continue
        while len(sentence) > MAX_CHARS:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if len(buf) + len(sentence) > MAX_CHARS and buf:
            pieces.append(buf)
            buf = ""
        buf += sentence
    if buf.strip():
        pieces.append(buf)
    return [piece.strip() for piece in pieces if piece.strip()]


def _pack(paragraphs: list[str]) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for paragraph in paragraphs:
        for piece in _split_long(paragraph):
            if buf and size + len(piece) > MAX_CHARS:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            buf.append(piece)
            size += len(piece)
            if size >= TARGET_CHARS:
                chunks.append("\n".join(buf))
                buf, size = [], 0
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def make_chunks(title: str, body_md: str) -> list[tuple[str, str]]:
    """返回 [(章节标题, 块正文)]，顺序即原文顺序。"""
    _frontmatter, body = split_frontmatter(body_md or "")
    body = strip_duplicate_title(body, title or "")
    sections, _structured = split_markdown_sections(body)
    found: list[tuple[str, str]] = []
    for section in sections:
        heading = ""
        match = _HEADING_LINE.match(section)
        if match:
            heading = markdown_to_text(match.group(1)).strip()[:200]
            section = section[match.end() :]
        text = markdown_to_text(section)
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
        for chunk in _pack(paragraphs):
            found.append((heading, chunk))
            if len(found) >= MAX_CHUNKS:
                return found
    return found


def replace_chunks(conn, article_id: int, title: str, body_md: str) -> int:
    chunks = make_chunks(title, body_md)
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s", (article_id,))
    if not chunks:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO article_chunks (article_id, ord, heading, body) VALUES (%s, %s, %s, %s)",
            [(article_id, index, heading, text) for index, (heading, text) in enumerate(chunks)],
        )
    return len(chunks)


def backfill_missing_chunks(conn, *, rebuild: bool = False) -> int:
    """给还没有块的文章切块。rebuild=True 时全部重切。"""
    where = "" if rebuild else "WHERE NOT EXISTS (SELECT 1 FROM article_chunks c WHERE c.article_id = a.id)"
    ids = [row["id"] for row in conn.execute(f"SELECT a.id FROM articles a {where} ORDER BY a.id").fetchall()]
    for article_id in ids:
        row = conn.execute("SELECT title, body_md FROM articles WHERE id = %s", (article_id,)).fetchone()
        replace_chunks(conn, article_id, row["title"], row["body_md"])
    conn.commit()
    return len(ids)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from researchlib.config import Settings
    from researchlib.db import prepare

    parser = argparse.ArgumentParser(description="重切全部文章的段落块")
    parser.parse_args(argv)
    conn = prepare(Settings.from_env())
    try:
        count = backfill_missing_chunks(conn, rebuild=True)
    finally:
        conn.close()
    print(f"已重切 {count} 篇文章的段落块")
    return 0


if __name__ == "__main__":
    sys.exit(main())

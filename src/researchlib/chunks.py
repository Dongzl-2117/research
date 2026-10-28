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
# 一篇最多切这么多块。译文块号从 10000 起，原文块号不能碰到它。
MAX_CHUNKS = 9000
OLD_MAX_CHUNKS = 400
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


def _pack_indexed(paragraphs: list[str]) -> list[tuple[int, str]]:
    """和 _pack 一样装箱，另外记每块从第几段开始。"""
    chunks: list[tuple[int, str]] = []
    buf: list[str] = []
    size = 0
    first: int | None = None
    for index, paragraph in enumerate(paragraphs):
        for piece in _split_long(paragraph):
            if buf and size + len(piece) > MAX_CHARS:
                chunks.append((first or 0, "\n".join(buf)))
                buf, size, first = [], 0, None
            if first is None:
                first = index
            buf.append(piece)
            size += len(piece)
            if size >= TARGET_CHARS:
                chunks.append((first, "\n".join(buf)))
                buf, size, first = [], 0, None
    if buf:
        chunks.append((first or 0, "\n".join(buf)))
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
    """原文的块。译文的块（lang = 'zh'）不动，译文重写时另行重切。切之前先去掉归档头和引流尾巴。"""
    from researchlib.boilerplate import clean_article

    row = conn.execute("SELECT source_id FROM articles WHERE id = %s", (article_id,)).fetchone()
    if row is not None:
        body_md = clean_article(conn, article_id, row["source_id"], body_md)
    chunks = make_chunks(title, body_md)
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s AND lang = ''", (article_id,))
    if not chunks:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO article_chunks (article_id, ord, heading, body) VALUES (%s, %s, %s, %s)",
            [(article_id, index, heading, text) for index, (heading, text) in enumerate(chunks)],
        )
    return len(chunks)


# 译文的块和原文的块放在同一张表里，块号从这里起，和原文的块号（最多 MAX_CHUNKS）不会撞。
TRANSLATION_ORD_BASE = 10000
_HTML_HEADING = re.compile(r"^\s*<h[1-6]\b", re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t ]+")


def _html_text(block: str) -> str:
    import html as html_lib

    text = _TAG.sub(" ", block.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n"))
    text = html_lib.unescape(text)
    return "\n".join(_SPACE.sub(" ", line).strip() for line in text.splitlines() if line.strip())


def make_chunks_from_html(body_html: str) -> list[tuple[str, str, str]]:
    """把译文 HTML 切成块：[(章节标题, 块正文, 对照页锚点)]。

    锚点按对照页的编号规则（非空顶层块依次编 p-1、p-2……），一块记它第一段的锚点。
    """
    from researchlib.translate.segments import top_level_blocks

    found: list[tuple[str, str, str]] = []
    heading = ""
    paragraphs: list[tuple[str, str]] = []  # (锚点, 文字)
    number = 0

    def flush() -> None:
        if not paragraphs:
            return
        texts = [text for _anchor, text in paragraphs]
        for first, chunk in _pack_indexed(texts):
            found.append((heading, chunk, paragraphs[first][0]))
        paragraphs.clear()

    for block in top_level_blocks(body_html or ""):
        # 编号规则和 translate.pair.stamp_paragraph_ids 一致：去掉标签后不为空的顶层块才编号。
        if not re.sub(r"\s+", " ", _TAG.sub("", block)).strip():
            continue
        number += 1
        anchor = f"p-{number}"
        text = _html_text(block)
        if not text:
            continue
        if _HTML_HEADING.match(block):
            flush()
            heading = text[:200]
            continue
        paragraphs.append((anchor, text))
        if len(found) >= MAX_CHUNKS:
            break
    flush()
    return found[:MAX_CHUNKS]


def replace_translation_chunks(conn, article_id: int) -> int:
    """按这篇的中文译文重切译文块。没有译文就只删旧的。"""
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s AND lang = 'zh'", (article_id,))
    row = conn.execute(
        "SELECT body_html FROM article_translations WHERE article_id = %s AND lang = 'zh'", (article_id,)
    ).fetchone()
    if row is None or not (row["body_html"] or "").strip():
        return 0
    chunks = make_chunks_from_html(row["body_html"])
    if not chunks:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO article_chunks (article_id, ord, heading, body, lang, anchor)"
            " VALUES (%s, %s, %s, %s, 'zh', %s)",
            [
                (article_id, TRANSLATION_ORD_BASE + index, heading, text, anchor)
                for index, (heading, text, anchor) in enumerate(chunks)
            ],
        )
    return len(chunks)


# 图片里的字（OCR）的块：lang = 'ocr'，块号从这里起，anchor 是文章页上那张图的 id。
OCR_ORD_BASE = 20000
OCR_HEADING = "图片里的字"
# 识别出的有效字（汉字、字母、数字）少于这个数的图不进检索：多半是装饰图、表情包或水印。
OCR_MIN_CHARS = 20
_MEANINGFUL = re.compile(r"[0-9A-Za-z一-鿿]")


def ocr_chunks(texts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """[(图片内容哈希, 识别出的字)] → [(图片锚点, 块正文)]，同一张图只算一次。

    先按微信对话规则整理（去可按住转文字等界面字、标方伟/网友），再切块进检索，
    与 articles.ocr_text / 阅读页旁路同一套 present。
    """
    from researchlib.render import image_anchor
    from researchlib.wechat_dialogue import present_ocr_pieces

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for digest, text in texts:
        if digest in seen:
            continue
        seen.add(digest)
        if len(_MEANINGFUL.findall(text or "")) < OCR_MIN_CHARS:
            continue
        cleaned = present_ocr_pieces([text or ""]) or (text or "")
        if len(_MEANINGFUL.findall(cleaned)) < OCR_MIN_CHARS:
            continue
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        for piece in _pack(lines):
            found.append((image_anchor(digest), piece))
            if len(found) >= MAX_CHUNKS:
                return found
    return found


def replace_ocr_chunks(conn, article_id: int) -> int:
    """按这篇挂着的图片和已经识别出的字重切图片文字块。重复的那一份不切。"""
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article_id,))
    article = conn.execute("SELECT duplicate_of FROM articles WHERE id = %s", (article_id,)).fetchone()
    if article is None or article["duplicate_of"] is not None:
        return 0
    rows = conn.execute(
        """
        SELECT i.content_hash, t.text
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        WHERE i.article_id = %s
        ORDER BY i.id
        """,
        (article_id,),
    ).fetchall()
    chunks = ocr_chunks([(row["content_hash"], row["text"]) for row in rows])
    if not chunks:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO article_chunks (article_id, ord, heading, body, lang, anchor)"
            " VALUES (%s, %s, %s, %s, 'ocr', %s)",
            [
                (article_id, OCR_ORD_BASE + index, OCR_HEADING, text, anchor)
                for index, (anchor, text) in enumerate(chunks)
            ],
        )
    return len(chunks)


def backfill_missing_chunks(conn, *, rebuild: bool = False) -> int:
    """给还没有块的文章切块，给有译文但还没有译文块的文章补切译文块。rebuild=True 时全部重切。

    重复的那一份（duplicate_of 不为空）不切块，检索只用主版本。
    """
    where = "WHERE a.duplicate_of IS NULL"
    if not rebuild:
        # 以前每篇最多切 400 块，切满的长文后面没进检索：正好 400 块的重切一遍。
        where += (
            " AND (NOT EXISTS (SELECT 1 FROM article_chunks c WHERE c.article_id = a.id AND c.lang = '')"
            f" OR (SELECT count(*) FROM article_chunks c WHERE c.article_id = a.id AND c.lang = '') = {OLD_MAX_CHUNKS})"
        )
    ids = [row["id"] for row in conn.execute(f"SELECT a.id FROM articles a {where} ORDER BY a.id").fetchall()]
    for article_id in ids:
        row = conn.execute("SELECT title, body_md FROM articles WHERE id = %s", (article_id,)).fetchone()
        replace_chunks(conn, article_id, row["title"], row["body_md"])
    translated_where = "WHERE a.duplicate_of IS NULL AND t.body_html <> ''"
    if not rebuild:
        translated_where += (
            " AND NOT EXISTS (SELECT 1 FROM article_chunks c WHERE c.article_id = a.id AND c.lang = 'zh')"
        )
    translated = [
        row["id"]
        for row in conn.execute(
            f"""
            SELECT a.id FROM articles a JOIN article_translations t ON t.article_id = a.id
            {translated_where}
            ORDER BY a.id
            """
        ).fetchall()
    ]
    for article_id in translated:
        replace_translation_chunks(conn, article_id)
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

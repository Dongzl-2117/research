"""补抽缺口：标题级提及、去重后的主版本要进队列；网友帖不进。OCR 旁路写进文章。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ocr import refresh_article_ocr
from researchlib.sources import load_sources
from researchlib.views import PROMPT_VERSION, gap_articles

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _store(conn, settings, source_id, external_id, title, body, raw_meta=None):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    doc = ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2026, 4, 1, 9, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta=raw_meta or {},
        image_refs=[],
    )
    store_documents(
        conn,
        source=sources[source_id],
        docs=[doc],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    return conn.execute(
        "SELECT id, content_hash FROM articles WHERE external_id = %s AND source_id = %s",
        (external_id, source_id),
    ).fetchone()


def test_gap_includes_title_mention_and_kept_original_but_not_guest(conn, settings):
    title_row = _store(conn, settings, "fangwei-mp", "gap-title", "贵州茅台还能看吗", "今天只写标题，正文先空着。\n")
    guest = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "gap-guest",
        "美亚光电和茅台",
        "读者转发：美亚光电的设备不错，顺带提到贵州茅台。\n",
        {"poster_name": "某读者", "poster_id": "8241"},
    )
    # 去重后留下的主版本：正文只有一句判断，提及可能是一带而过。
    kept = _store(
        conn,
        settings,
        "gudujinhuati",
        "gap-kept",
        "随手记",
        "今天市场很热闹。\n\n我还没有那么了解茅台。\n\n先观察到这里。\n",
    )
    duplicate = _store(
        conn,
        settings,
        "gudujinhuati",
        "gap-dup",
        "随手记",
        "今天市场很热闹。\n\n我还没有那么了解茅台。\n\n先观察到这里。\n",
    )
    conn.execute("UPDATE articles SET duplicate_of = %s WHERE id = %s", (kept["id"], duplicate["id"]))
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance, about)
        VALUES (%s, %s, %s, %s, '', '', true)
        """,
        (title_row["id"], company, title_row["content_hash"], PROMPT_VERSION),
    )
    conn.commit()
    guest_meta = conn.execute("SELECT raw_meta FROM articles WHERE id = %s", (guest["id"],)).fetchone()["raw_meta"]
    assert guest_meta["poster_role"] == "guest"
    rows = gap_articles(conn, [company], ["fangwei-mp", "fangwei-zsxq", "gudujinhuati"], 20)
    found = {row["id"] for row in rows}
    assert title_row["id"] in found
    assert kept["id"] in found
    assert guest["id"] not in found
    assert duplicate["id"] not in found


def test_ocr_side_field_is_what_views_read(conn, settings):
    row = _store(conn, settings, "fangwei-zsxq", "ocr-post", "图片", "（见图）\n")
    conn.execute(
        """
        INSERT INTO image_texts (content_hash, engine, text) VALUES ('abc', 'test', %s)
        """,
        ("茅台批价这一周没动，渠道仍在观望，短期不宜提价。",),
    )
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        VALUES (%s, 'fangwei-zsxq', 'pic.png', 'pic.png', 'abc')
        """,
        (row["id"],),
    )
    digest = refresh_article_ocr(conn, row["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text, ocr_hash FROM articles WHERE id = %s", (row["id"],)).fetchone()
    assert "不宜提价" in saved["ocr_text"] and saved["ocr_hash"] == digest
    # 空白观点在 OCR 哈希对不上时会重新进入缺口。
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO article_mentions (article_id, company_id, depth, hit_count)
        VALUES (%s, %s, 'title', 1)
        ON CONFLICT (article_id, company_id) DO UPDATE SET depth = 'title'
        """,
        (row["id"], company),
    )
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance, ocr_hash)
        VALUES (%s, %s, %s, %s, '', '', '')
        """,
        (row["id"], company, row["content_hash"], PROMPT_VERSION),
    )
    conn.commit()
    found = {item["id"] for item in gap_articles(conn, [company], ["fangwei-zsxq"], 10)}
    assert row["id"] in found

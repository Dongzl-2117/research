"""重切 OCR 块后，观点原句不能沿用被邻图复用的块号。只用合成文字。"""

from datetime import UTC, datetime

from researchlib.chunks import OCR_ORD_BASE, replace_ocr_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.views import timeline

QUOTE = "合成公司的组织优势需要持续经营和完善供应链才能转化为利润。"
REMOVED = "合成促销卡片已经失效，不属于正文中的投资判断。"


def test_rechunk_relocates_quote_and_clears_removed_reference(conn, settings, monkeypatch):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-mp")
    store_documents(
        conn, source=source,
        docs=[ParsedDoc("synthetic-references", "合成引用测试", datetime(2026, 1, 1, tzinfo=UTC),
                        "正文提到腾讯、苹果和茅台，用合成语句测试引用。", {}, [])],
        data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[],
    )
    article = conn.execute(
        "SELECT id, content_hash FROM articles WHERE external_id = 'synthetic-references'"
    ).fetchone()
    article_id = article["id"]
    company_ids = [row["id"] for row in conn.execute("SELECT id FROM companies ORDER BY id LIMIT 3").fetchall()]
    old = [("img-aaaaaaaaaaaa", REMOVED), ("img-bbbbbbbbbbbb", QUOTE),
           ("img-cccccccccccc", "另一张图说明市场规模和其他完全不同的业务情况。")]
    for index, (anchor, body) in enumerate(old):
        conn.execute(
            "INSERT INTO article_chunks (article_id, ord, heading, body, lang, anchor)"
            " VALUES (%s, %s, '图片里的字', %s, 'ocr', %s)",
            (article_id, OCR_ORD_BASE + index, body, anchor),
        )
    for company, ordinal, quote in (
        (company_ids[0], OCR_ORD_BASE + 1, QUOTE),
        (company_ids[1], OCR_ORD_BASE, REMOVED),
        (company_ids[2], 0, "正文提到腾讯、苹果和茅台"),
    ):
        conn.execute(
            "INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance,"
            " quote, chunk_ord) VALUES (%s, %s, %s, 2, '合成判断。', 'positive', %s, %s)",
            (article_id, company, article["content_hash"], quote, ordinal),
        )
    conn.commit()
    monkeypatch.setattr("researchlib.chunks._article_ocr_chunks", lambda *_args, **_kwargs: old[1:])
    assert replace_ocr_chunks(conn, article_id) == 2
    references = {row["company_id"]: row["chunk_ord"] for row in conn.execute(
        "SELECT company_id, chunk_ord FROM company_views WHERE article_id = %s", (article_id,),
    ).fetchall()}
    assert references == {company_ids[0]: OCR_ORD_BASE, company_ids[1]: None, company_ids[2]: 0}
    items = timeline(conn, company_ids[0], {source.id: source}, settings)
    assert items[0]["quote"] == QUOTE
    assert items[0]["chunk"] == OCR_ORD_BASE and items[0]["anchor"] == "img-bbbbbbbbbbbb"
    # 已经发生过错号的旧观点也不能读出邻图。读取只撤掉定位，不改观点和库内旧号。
    conn.execute("UPDATE company_views SET chunk_ord = %s WHERE article_id = %s AND company_id = %s",
                 (OCR_ORD_BASE + 1, article_id, company_ids[0]))
    items = timeline(conn, company_ids[0], {source.id: source}, settings)
    assert items[0]["quote"] == QUOTE and items[0]["chunk"] is None and items[0]["anchor"] == ""
    assert replace_ocr_chunks(conn, article_id) == 2
    assert timeline(conn, company_ids[0], {source.id: source}, settings)[0]["chunk"] == OCR_ORD_BASE

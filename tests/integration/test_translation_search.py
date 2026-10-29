"""英文文章的中文译文进检索：中文问题也能找到它，引用落到对照页那一段。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.chunks import TRANSLATION_ORD_BASE, make_chunks_from_html, replace_translation_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import retrieve
from researchlib.sources import enabled_sources, load_sources
from researchlib.translate.pair import stamp_paragraph_ids
from researchlib.translate.service import list_pending_english
from researchlib.web.app import create_app
from researchlib.web.queries import Scope

SHANGHAI = ZoneInfo("Asia/Shanghai")

ENGLISH = """
## Pricing

Apple keeps raising iPhone prices and demand holds up, because the switching costs of the ecosystem are high.

## Services

The App Store take rate is the real moat; regulators are the main risk to that business.
"""

ZH_HTML = (
    "<h2>定价</h2>"
    "<p>苹果一直在给 iPhone 涨价，需求却没掉，因为生态的转换成本很高。</p>"
    "<h2>服务</h2>"
    "<p>应用商店的抽成比例才是真正的护城河；监管是这块业务最大的风险。</p>"
)


def _english(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    store_documents(
        conn,
        source=sources["gudujinhuati"],
        docs=[
            ParsedDoc(
                external_id="en:apple",
                title="Apple's pricing power",
                published_at=datetime(2025, 5, 2, 8, 0, tzinfo=SHANGHAI),
                body_md=ENGLISH,
                raw_meta={"language": "en"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    article_id = conn.execute("SELECT id, content_hash FROM articles WHERE external_id = 'en:apple'").fetchone()
    conn.execute(
        """
        INSERT INTO article_translations (article_id, content_hash, title_zh, body_html, body_text, complete, segments)
        VALUES (%s, %s, '苹果的定价权', %s, %s, true, 4)
        """,
        (article_id["id"], article_id["content_hash"], ZH_HTML, ZH_HTML),
    )
    replace_translation_chunks(conn, article_id["id"])
    conn.commit()
    return article_id["id"]


def test_html_chunks_carry_pair_anchors():
    chunks = make_chunks_from_html(ZH_HTML)
    assert [heading for heading, _text, _anchor in chunks] == ["定价", "服务"]
    stamped = stamp_paragraph_ids(ZH_HTML)
    for _heading, text, anchor in chunks:
        # 锚点指向对照页上真有这段话的那一块。
        block = stamped.split(f'id="{anchor}"', 1)[1].split("</p>", 1)[0]
        assert text[:8] in block


def test_chinese_question_finds_the_translation(conn, settings):
    article_id = _english(conn, settings)
    zh = conn.execute(
        "SELECT ord, anchor FROM article_chunks WHERE article_id = %s AND lang = 'zh' ORDER BY ord", (article_id,)
    ).fetchall()
    assert zh and zh[0]["ord"] == TRANSLATION_ORD_BASE and zh[0]["anchor"].startswith("p-")
    sources = enabled_sources(load_sources(settings.sources_path))
    scope = Scope(source_ids=[source.id for source in sources])
    found = retrieve(conn, "苹果的护城河和监管风险", scope, sources, purpose="llm")
    assert found.passages
    assert all(p.lang == "zh" for p in found.passages if p.article_id == article_id)
    assert any(p.article_id == article_id for p in found.passages)
    passage = next(p for p in found.passages if p.article_id == article_id)
    assert passage.title == "苹果的定价权"
    assert "#p-" in passage.url
    with TestClient(create_app(settings)) as client:
        page = client.get("/search", params={"q": "应用商店 抽成"}).text
        assert f'href="/articles/{article_id}?chunk={TRANSLATION_ORD_BASE + 1}#p-' in page
        assert "1 篇含全部检索词" in page
        api = client.get("/api/v1/search", params={"q": "苹果的护城河", "level": "chunk"}).json()
        assert api["passages"][0]["lang"] == "zh"


def test_pending_english_query_runs_and_skips_translated(conn, settings):
    article_id = _english(conn, settings)
    assert article_id not in list_pending_english(conn, limit=10)
    conn.execute("DELETE FROM article_translations WHERE article_id = %s", (article_id,))
    conn.commit()
    assert article_id in list_pending_english(conn, limit=10)


def test_translation_html_is_filtered_on_the_article_page(conn, settings):
    """译文是模型写的 HTML，和原文一样过白名单。"""
    article_id = _english(conn, settings)
    conn.execute(
        "UPDATE article_translations SET body_html = %s WHERE article_id = %s",
        (
            '<h2>定价</h2><p>涨价<img src=x onerror="alert(1)"></p>'
            '<p title="&quot; onmouseover=alert(2) x=&quot;">需求<script>alert(3)</script></p>',
            article_id,
        ),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}/zh").text
    assert "涨价" in page and "需求" in page
    assert "onerror" not in page
    assert "alert(3)" not in page
    assert 'title="" onmouseover' not in page

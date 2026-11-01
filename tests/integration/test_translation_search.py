"""英文文章的中文译文进检索：中文问题也能找到它，引用落到对照页那一段。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib import retrieval
from researchlib.chunks import TRANSLATION_ORD_BASE, make_chunks_from_html, replace_translation_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import retrieve
from researchlib.sources import enabled_sources, load_sources
from researchlib.translate.language import is_chinese
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
        assert "1 篇全部命中" in page
        api = client.get("/api/v1/search", params={"q": "苹果的护城河", "level": "chunk"}).json()
        assert api["passages"][0]["lang"] == "zh"


def test_pending_english_query_runs_and_skips_translated(conn, settings):
    article_id = _english(conn, settings)
    assert article_id not in list_pending_english(conn, limit=10)
    conn.execute("DELETE FROM article_translations WHERE article_id = %s", (article_id,))
    conn.commit()
    assert article_id in list_pending_english(conn, limit=10)


def _store(conn, settings, source_id, docs):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    store_documents(
        conn,
        source=sources[source_id],
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )


def test_pending_english_is_found_behind_newer_chinese(conn, settings):
    """不带 --source 时最新的几乎全是中文：要先筛掉中文再取前 N 篇，不然取不到英文。"""
    article_id = _english(conn, settings)
    conn.execute("DELETE FROM article_translations WHERE article_id = %s", (article_id,))
    conn.commit()
    _store(
        conn,
        settings,
        "fangwei-mp",
        [
            ParsedDoc(
                external_id=f"mp/{index}",
                title=f"第 {index} 篇",
                published_at=datetime(2025, 6, 1 + index, 8, 0, tzinfo=SHANGHAI),
                body_md=f"第 {index} 篇讲腾讯第 {index} 个季度的广告收入和视频号的库存，和别的篇不一样。",
                raw_meta={},
                image_refs=[],
            )
            for index in range(3)
        ],
    )
    assert list_pending_english(conn, limit=1) == [article_id]
    assert list_pending_english(conn, limit=1, source_id="fangwei-mp") == []


LANGUAGE_SAMPLES = {
    "纯中文": ("腾讯第三季度的广告收入同比增长，视频号的库存还在放量。", {}),
    "中文夹英文": ("苹果的 App Store 抽成是护城河，Services 收入的占比还在提高。", {}),
    "英文夹中文": ("Apple raised iPhone prices again and demand held up (护城河).", {}),
    "纯英文": ("Netflix keeps adding subscribers while raising prices in most markets.", {}),
    "标了中文": ("Amazon Web Services margins expanded on lower depreciation.", {"language": "zh"}),
    "标了英文": ("拼多多的海外业务继续亏损，但亏损在收窄。", {"language": "en"}),
    "只有数字": ("2025 1234 5678 9012", {}),
}


def test_pending_english_sql_agrees_with_is_chinese(conn, settings):
    """待译队列在 SQL 里判中英文，和 is_chinese 拿同一批文章对拍。"""
    _store(
        conn,
        settings,
        "gudujinhuati",
        [
            ParsedDoc(
                external_id=f"lang:{name}",
                title=name,
                published_at=datetime(2025, 7, 1 + index, 8, 0, tzinfo=SHANGHAI),
                body_md=body,
                raw_meta=meta,
                image_refs=[],
            )
            for index, (name, (body, meta)) in enumerate(LANGUAGE_SAMPLES.items())
        ],
    )
    rows = conn.execute(
        "SELECT id, title, body_text, body_md, raw_meta FROM articles WHERE external_id LIKE 'lang:%'"
    ).fetchall()
    assert len(rows) == len(LANGUAGE_SAMPLES)
    expected = {
        row["id"]
        for row in rows
        if not is_chinese(row["body_text"] or row["body_md"] or "", language=row["raw_meta"].get("language"))
    }
    pending = set(list_pending_english(conn, limit=100))
    assert pending == expected
    titles = {row["id"]: row["title"] for row in rows}
    assert {titles[article_id] for article_id in pending} == {"英文夹中文", "纯英文", "标了英文", "只有数字"}


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


def test_br_line_paragraph_pairs_line_by_line_on_the_page(conn, settings):
    """22092 的形状：英文一个 <p> 里塞几十行 <br>，对照页要一行一对，不是一整块两对。"""
    count = 20
    english = "\n".join(f"Line {index} of the newsletter digest keeps its own row." for index in range(count))
    zh_html = "<p>" + "<br>".join(f"第{index}行，这一行在对照页上单独成对。" for index in range(count)) + "</p>"
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    store_documents(
        conn,
        source=sources["gudujinhuati"],
        docs=[
            ParsedDoc(
                external_id="en:br-lines",
                title="A digest of one paragraph",
                published_at=datetime(2025, 5, 3, 8, 0, tzinfo=SHANGHAI),
                body_md=english,
                raw_meta={"language": "en"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    row = conn.execute("SELECT id, content_hash FROM articles WHERE external_id = 'en:br-lines'").fetchone()
    conn.execute(
        """
        INSERT INTO article_translations (article_id, content_hash, title_zh, body_html, body_text, complete, segments)
        VALUES (%s, %s, '一段里的日报', %s, %s, true, %s)
        """,
        (row["id"], row["content_hash"], zh_html, zh_html, count),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}").text
    assert page.count('class="prose bilingual-en with-toolbar"') == count
    assert page.count('class="prose bilingual-zh with-toolbar"') == count
    assert "第0行，这一行在对照页上单独成对。" in page
    assert "第7行，这一行在对照页上单独成对。" in page


def test_quoted_english_phrase_keeps_the_original_passage(conn, settings, monkeypatch):
    """引号里的英文原句只在原文块里。语义召回带进来同一篇的译文块时，原文块不能被「只留译文」丢掉：
    线上搜「"Listen to this Update in your podcast player"」侧栏数到 38 篇，结果一篇没有。"""
    article_id = _english(conn, settings)
    # 语义这一路把「服务」那节的译文块召回进来（线上是向量近邻，这里直接给）。
    monkeypatch.setattr(retrieval.embeddings, "ready", lambda _conn: "vector")
    monkeypatch.setattr(retrieval.embeddings, "query_vector", lambda _text: [0.1, 0.2, 0.3])
    monkeypatch.setattr(
        retrieval, "_vector_lookup", lambda *_args: {(article_id, TRANSLATION_ORD_BASE + 1): 0.9}
    )
    with TestClient(create_app(settings)) as client:
        quoted = client.get("/api/v1/search", params={"q": '"take rate is the real moat"'}).json()
        # 「moat; regulators」：切词以后两个词挨着，原文中间隔着分号，不算原样出现。
        missing = client.get("/api/v1/search", params={"q": '"real moat regulators"'}).json()
    assert quoted["total"] == 1 and [item["id"] for item in quoted["items"]] == [article_id]
    assert "real moat" in quoted["items"][0]["snippet"]
    # 词挨着、句子却不是这样写的，不算命中，也不数。
    assert missing["total"] == 0 and missing["items"] == []

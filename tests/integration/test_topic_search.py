"""片段搜索契约。向量是固定测试向量，只验证融合与归属，不代表真实语义效果。"""
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from researchlib import embeddings
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import reset_stats_cache, search_articles
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope


def _store(conn, settings, key, title, blocks):
    source = next(s for s in load_sources(settings.sources_path) if s.id == "fangwei-mp")
    body = "\n\n".join(f"## {h}\n\n{b}" for h, b in blocks)
    store_documents(
        conn, source=source,
        docs=[ParsedDoc(key, title, datetime(2026, 1, 1, tzinfo=UTC), body, {}, [])],
        data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[],
    )
    aid = conn.execute("SELECT id FROM articles WHERE external_id=%s", (key,)).fetchone()["id"]
    # 控制边界，让跨段、锚点和相邻去重的断言不依赖装箱长度。
    conn.execute("DELETE FROM article_chunks WHERE article_id=%s", (aid,))
    for i, (heading, text) in enumerate(blocks):
        conn.execute("INSERT INTO article_chunks(article_id,ord,heading,body) VALUES(%s,%s,%s,%s)",
                     (aid, i, heading, text))
    conn.commit()
    reset_stats_cache()
    return aid


def _search(conn, settings, text):
    sources = enabled_sources(load_sources(settings.sources_path))
    return search_articles(conn, text, Scope(source_ids=[s.id for s in sources]), sources)


def test_company_title_does_not_own_other_company_facts(conn, settings):
    good = _store(conn, settings, "positive", "仓储经营", [
        ("机制", "好市多的商业模式依靠会员费与商品低毛利，较快周转减少资金占用。"),
    ])
    bad = _store(conn, settings, "foreign", "Costco 商业模式", [
        ("同行", "Walmart 的商业模式依赖门店销售，沃尔玛通过自己的供应链组织库存。"),
    ])
    _store(conn, settings, "only-name", "Costco 新店", [("开业", "Costco 新店举行了停车路线说明会。")])
    result = _search(conn, settings, "Costco 商业模式")
    assert result.hits[0].article_id == good
    assert bad not in {h.article_id for h in result.hits}
    assert "会员费" in result.hits[0].passages[0].body


def test_local_subject_and_adjacent_hits_form_one_window(conn, settings):
    aid = _store(conn, settings, "context", "仓储经营研究", [
        ("经营结构", "Costco 是这一节讨论的公司，下面看它的收入来源。"),
        ("经营结构", "它的商业模式靠会员费形成持续收入，商品薄利换来较高续费率。"),
        ("经营结构", "快速卖出商品减少库存占用，采购规模又使低价能够维持。"),
    ])
    result = _search(conn, settings, "开市客 商业模式")
    hit = next(h for h in result.hits if h.article_id == aid)
    assert len(hit.passages) == 1
    p = hit.passages[0]
    assert p.ord == 1 and p.entity_basis == "上文"
    assert "Costco" in p.before and "会员费" in p.body
    assert p.url == f"/articles/{aid}?chunk=1"


def test_vector_topic_query_and_entity_filter_are_separate(conn, settings):
    good = _store(conn, settings, "semantic-good", "Costco economics", [
        ("Economics", "Costco collects annual membership dues and sells goods at very low margins."),
    ])
    bad = _store(conn, settings, "semantic-foreign", "Costco industry", [
        ("Other retailer", "Walmart 的商业模式依靠商品销售，这些事实属于沃尔玛。"),
    ])
    seen = []
    def fixed_vectors(texts):
        seen.extend(texts)
        return [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts]
    embeddings.use_embedder(fixed_vectors, model="contract-only")
    try:
        assert embeddings.backfill(conn) == 2
        seen.clear()
        result = _search(conn, settings, "Costco 商业模式")
        assert seen == ["商业模式"]
        assert result.hits[0].article_id == good and result.hits[0].semantic
        assert bad not in {h.article_id for h in result.hits}
    finally:
        embeddings.use_embedder(None)


def test_each_visible_excerpt_links_its_own_chunk(conn, settings):
    aid = _store(conn, settings, "two-windows", "Costco 经营笔记", [
        ("收费", "Costco 的商业模式通过会员费回收固定成本，商品低毛利促进续费。"),
        ("行业", "Walmart 的商业模式依靠广泛门店，本文在这段改谈沃尔玛。"),
        ("周转", "Costco 的商业模式通过较快周转减少库存占用，精选品种方便集中采购。"),
    ])
    with TestClient(create_app(settings)) as client:
        html = client.get("/search", params={"q": "Costco 商业模式"}).text
    assert f'href="/articles/{aid}?chunk=0">查看这段原文' in html
    assert f'href="/articles/{aid}?chunk=2">查看这段原文' in html
    assert "本文在这段改谈沃尔玛" not in html

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


def test_title_cannot_reattribute_following_pronoun_after_foreign_subject(conn, settings):
    bad = _store(conn, settings, "pronoun-foreign", "Costco 经营研究", [
        ("同行", "Walmart 的经营安排是本段的主角。"),
        ("现金", "它的商业模式依赖商品价差，这些收入来自它的门店销售。"),
    ])
    result = _search(conn, settings, "Costco 商业模式")
    assert bad not in {hit.article_id for hit in result.hits}


def test_foreign_heading_overrides_prior_target_and_document_title(conn, settings):
    bad = _store(conn, settings, "heading-foreign", "Costco 经营研究", [
        ("对象", "Costco 是上一节观察的对象。"),
        ("Walmart 经营结构", "它的商业模式依赖门店销售与物流收费。"),
    ])
    with TestClient(create_app(settings)) as client:
        html = client.get("/search", params={"q": "Costco 商业模式"}).text
    assert f'href="/articles/{bad}?chunk=1' not in html
    assert "依赖门店销售与物流收费" not in html


def test_repeated_query_label_does_not_outrank_literal_assertion(conn, settings):
    good = _store(conn, settings, "assertion", "收费结构", [
        ("收入", "Costco 的商业模式依靠会员持续付费，让商品定价留出较小差额。"),
    ])
    _store(conn, settings, "label", "文件题名", [
        ("目录", "Costco 商业模式。Costco 商业模式。Costco 商业模式。"),
    ])
    result = _search(conn, settings, "Costco 商业模式")
    assert result.hits[0].article_id == good


def test_cross_article_copy_does_not_take_two_visible_slots(conn, settings):
    text = "Costco 的商业模式依靠持续会费，商品定价保持较小差额，使顾客愿意再次续费。"
    _store(conn, settings, "copy-a", "收费观察甲", [("收费", text)])
    _store(conn, settings, "copy-b", "收费观察乙", [("收费", text), ("附录", "另记归档日期。")])
    result = _search(conn, settings, "Costco 商业模式")
    assert sum(text in (p.excerpt or p.body) for h in result.hits for p in h.passages) == 1
    assert not any(p.semantic_hit for hit in result.hits for p in hit.passages)


def test_same_pronoun_sentence_keeps_different_numeric_context(conn, settings):
    text = "它的商业模式就是上文所列比例，续约带来的现金流成为后续合同收入的来源。"
    for key, ratio in (("ratio-a", "90%"), ("ratio-b", "95%")):
        _store(conn, settings, key, "收费札记", [
            ("收费", f"Costco 这一比例本年度为 {ratio}。"), ("收费", text),
        ])
    result = _search(conn, settings, "Costco 商业模式")
    assert len(result.hits) == 2
    contexts = {hit.passages[0].before for hit in result.hits}
    assert any("90%" in context for context in contexts)
    assert any("95%" in context for context in contexts)


def test_chunk_semantics_cannot_label_a_different_local_sentence(conn, settings, monkeypatch):
    aid = _store(conn, settings, "semantic-reference", "日常记录", [
        ("事项", "Costco 本周搬入新的办公区。Costco 商业模式。"),
    ])
    def fixed_candidates(_conn, _query, _scope, rows):
        # 固定块分数仅验证证据边界，不能当作真实模型质量测试。
        return rows, {(aid, 0): 0.9}
    monkeypatch.setattr("researchlib.retrieval.semantic_candidates", fixed_candidates)
    result = _search(conn, settings, "Costco 商业模式")
    assert result.hits and not result.hits[0].semantic
    assert not any(p.semantic_hit for hit in result.hits for p in hit.passages)

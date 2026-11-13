"""检索快不快：规划器对「可见文章」的估计、应用连接的设置、检索页的计数不查两遍、向量块数过期时不挡这次搜索。
结果都要和以前一样。"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from researchlib import embeddings
from researchlib.db import connect
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import reset_stats_cache
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web import routes
from researchlib.web.app import create_app

FANGWEI = "fangwei-mp"
FEISHU = "linwenfeng-feishu"


def _store(conn, settings, source_id, docs):
    source = next(item for item in load_sources(settings.sources_path) if item.id == source_id)
    store_documents(
        conn, source=source, docs=docs, data_dir=settings.data_dir, archive_files=len(docs), images_in_archive=0,
        errors=[],
    )
    reset_stats_cache()


def _doc(external_id, title, body, *, month=1, feishu=False):
    meta = {"format": "markdown"}
    if feishu:
        meta.update({"parent": "docs", "filename": external_id.rsplit("/", 1)[-1]})
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=None if feishu else datetime(2024, month, 1, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta=meta,
        image_refs=[],
    )


def test_planner_knows_how_many_articles_are_visible(conn):
    # 迁移 036：「可见文章」的条件（visibility.visible_sql）有了统计。以前按默认的 0.5% 估，几百篇估成一两篇，
    # 召回段落时选逐篇的嵌套循环，每篇各查一遍全文索引；一万篇、没有隐藏文章的库上一条 SQL 要七到十秒。
    conn.execute(
        "INSERT INTO articles (source_id, external_id, content_hash, title, body_md, body_text, raw_meta)"
        " SELECT %s, 'speed-' || g, md5(g::text), '标题', '正文', '正文',"
        " CASE WHEN g <= 5 THEN '{\"hidden\": \"guest\"}'::jsonb ELSE '{}'::jsonb END"
        " FROM generate_series(1, 400) g",
        (FANGWEI,),
    )
    conn.commit()
    conn.execute("ANALYZE articles")
    plan = conn.execute(
        "EXPLAIN (FORMAT JSON) SELECT id FROM articles a WHERE coalesce(a.raw_meta->>'hidden', '') = ''"
    ).fetchone()
    estimate = plan["QUERY PLAN"][0]["Plan"]["Plan Rows"]
    assert 300 < estimate <= 400


def test_app_connections_scan_tables_from_the_start_without_jit(settings):
    # 一路全文召回取命中的前 FTS_POOL 块、不排序。大表的顺序扫描默认从上一次停下的地方接着扫，命中超过这么多块时
    # 同一个词连搜两次结果会不一样；应用的连接让它每次从表头开始。JIT 关掉省下每条查询现场编译的时间。
    with connect(settings) as other:
        assert other.execute("SHOW synchronize_seqscans").fetchone()["synchronize_seqscans"] == "off"
        assert other.execute("SHOW jit").fetchone()["jit"] == "off"


def test_search_page_counts_sources_once_and_shows_the_same_numbers(conn, settings, monkeypatch):
    _store(
        conn, settings, FANGWEI,
        [
            _doc("fee", "开市客笔记", "Costco 靠会员费赚钱，会员费几乎就是利润。", month=1),
            _doc("sam", "山姆笔记", "山姆会员店也收会员费，续费率很高。", month=2),
        ],
    )
    _store(conn, settings, FEISHU, [_doc("docs/会员制__x.md", "会员制", "会员费模式的零售商。", feishu=True)])
    calls: list[int] = []
    counted = routes.full_match_counts

    def counting(*args, **kwargs):
        calls.append(1)
        return counted(*args, **kwargs)

    monkeypatch.setattr(routes, "full_match_counts", counting)
    with TestClient(create_app(settings)) as client:

        def page(**params) -> str:
            return client.get("/search", params={"q": "会员费", **params}).text

        everything = page()
        # 没选来源：分面要的计数就是检索时数过的那一条，不再查一遍。
        assert calls == []
        assert '<span>全部来源</span><span class="num">3</span>' in everything
        picked = page(source=FANGWEI)
        # 选了来源：分面照旧数全部来源。
        assert calls == [1]
        # 每次都再数一遍的老办法，得到一模一样的页面。
        search = routes.search_articles
        monkeypatch.setattr(routes, "search_articles", lambda *a, **k: replace(search(*a, **k), scope=None))
        assert page() == everything
        assert page(source=FANGWEI) == picked


def test_stale_vector_count_is_shown_while_recounted_in_background(conn, settings):
    if embeddings.column_type(conn) is None:
        pytest.skip("测试库没有 pgvector")
    embeddings.use_embedder(lambda texts: [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts])
    try:
        _store(conn, settings, FANGWEI, [_doc("fee", "开市客笔记", "Costco 靠会员费赚钱，商品几乎不赚毛利。")])
        actual = embeddings.coverage(conn)
        assert actual[0] == actual[1] > 0
        key = conn.info.dbname
        # 这个进程还没数过：当场数。
        assert embeddings._cached_coverage(conn) == actual
        # 上次数是十分钟以前（那时还没算完）：这次先用上次的数，不在这次搜索里把全库再过一遍；后台另开连接重数。
        embeddings._state.coverage[key] = ((0, actual[1]), time.monotonic() - embeddings.COVERAGE_TTL_S - 1)
        assert embeddings._cached_coverage(conn) == (0, actual[1])
        deadline = time.monotonic() + 10
        while embeddings._state.coverage[key][0] != actual:
            assert time.monotonic() < deadline, "后台没有重数"
            time.sleep(0.05)
        assert embeddings._cached_coverage(conn) == actual
    finally:
        embeddings.use_embedder(None)


def test_vector_count_is_kept_fresh_without_anyone_searching(conn, settings):
    # 11-13：两天没人搜，第一次搜索报的还是两天前的「向量还没算完（218554/236318 块）」，缺口其实早补完了。
    # 网站每分钟调一次 keep_coverage_fresh：隔五分钟重数一遍，不等有人来搜。
    if embeddings.column_type(conn) is None:
        pytest.skip("测试库没有 pgvector")
    opened: list[int] = []

    def open_conn():
        opened.append(1)
        return connect(settings)

    embeddings.use_embedder(lambda texts: [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts])
    try:
        _store(conn, settings, FANGWEI, [_doc("fee", "开市客笔记", "Costco 靠会员费赚钱，商品几乎不赚毛利。")])
        actual = embeddings.coverage(conn)
        assert actual[0] == actual[1] > 0
        key = conn.info.dbname
        two_days_ago = time.monotonic() - 2 * 86400
        embeddings._state.coverage[key] = ((0, actual[1] * 2), two_days_ago)
        assert embeddings.keep_coverage_fresh(open_conn)
        assert embeddings._state.coverage[key][0] == actual
        # 接着来的第一次搜索就是新数，不报没算完。
        assert embeddings.status_note(conn) == ""
        # 刚数过：下一分钟不再数，连接也不开。
        assert not embeddings.keep_coverage_fresh(open_conn)
        assert opened == [1]
        embeddings._state.coverage_counted -= embeddings.COVERAGE_TTL_S / 2
        assert embeddings.keep_coverage_fresh(open_conn)
        assert opened == [1, 1]
    finally:
        embeddings.use_embedder(None)
    # 没开语义检索：不数。
    assert not embeddings.keep_coverage_fresh(open_conn)
    assert opened == [1, 1]


def test_sample_banner_needs_every_article_to_be_a_sample(conn):
    from researchlib.web.queries import sample_state

    # 每一页都问一次，找到一篇不是示例的就停；结果和把文章全数一遍一样。
    def add(external_id, meta):
        conn.execute(
            "INSERT INTO articles (source_id, external_id, content_hash, title, body_md, body_text, raw_meta)"
            " VALUES (%s, %s, md5(%s), '标题', '正文', '正文', %s::jsonb)",
            (FANGWEI, external_id, external_id, meta),
        )

    assert not sample_state(conn, [FANGWEI])
    add("s1", '{"sample": "true"}')
    add("s2", '{"sample": true}')
    assert sample_state(conn, [FANGWEI])
    # 别的来源的文章不算。
    assert not sample_state(conn, [FEISHU])
    add("real", '{"sample": "false"}')
    assert not sample_state(conn, [FANGWEI])
    conn.execute("DELETE FROM articles WHERE external_id = 'real'")
    add("plain", "{}")
    assert not sample_state(conn, [FANGWEI])

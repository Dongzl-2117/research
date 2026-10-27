"""只在 research_test 合成数据上验证部署锁、读取边界和过期向量。"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from researchlib import embeddings
from researchlib.coordination import LOCK_KEY, DeploymentBusy, enter_task
from researchlib.db import connect, prepare
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import _vector_keys, _vector_lookup
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web import routes
from researchlib.web.app import create_app
from researchlib.web.queries import Scope


def _article(conn, settings, source_id="fangwei-mp", meta=None):
    source = next(s for s in load_sources(settings.sources_path) if s.id == source_id)
    doc = ParsedDoc(
        "review-" + source_id,
        "腾讯的稳定收入",
        datetime(2026, 1, 1, tzinfo=SHANGHAI),
        "腾讯的广告依靠稳定的客户续费。腾讯的游戏收入保持稳定。腾讯也有新的广告库存。",
        meta or {},
        [],
    )
    store_documents(
        conn, source=source, docs=[doc], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[]
    )
    return conn.execute("SELECT * FROM articles WHERE external_id=%s", (doc.external_id,)).fetchone()


def test_database_lock_defers_deployment_and_prevents_new_jobs(conn, settings):
    # conn 的 prepare 持有任务共享锁，另一个独立连接不能拿部署独占锁。
    other = connect(settings)
    try:
        assert not other.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]
        conn.execute("SELECT pg_advisory_unlock_all()")
        assert other.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]
        with pytest.raises(DeploymentBusy):
            enter_task(conn)
    finally:
        other.close()
    enter_task(conn)


def test_web_startup_is_readonly_and_pending_migration_blocks_it(conn, settings):
    # 修改会触发旧版 prepare 重算的状态，默认启动不得改动它。
    _article(conn, settings)
    before = conn.execute("SELECT id,raw_meta,content_hash FROM articles ORDER BY id").fetchall()
    ready = prepare(settings, maintenance=False)
    ready.close()
    assert conn.execute("SELECT id,raw_meta,content_hash FROM articles ORDER BY id").fetchall() == before
    conn.execute("DELETE FROM schema_migrations WHERE version='023_drop_demo_article.sql'")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="禁止自动迁移或降级"):
            prepare(settings, maintenance=False)
        assert (
            conn.execute("SELECT 1 FROM schema_migrations WHERE version='023_drop_demo_article.sql'").fetchone() is None
        )
    finally:
        conn.execute("INSERT INTO schema_migrations(version) VALUES ('023_drop_demo_article.sql')")
        conn.commit()


def test_company_get_is_readonly_even_with_old_auto_switch(conn, settings, monkeypatch):
    _article(conn, settings)
    company = conn.execute("SELECT id FROM companies WHERE name_zh='腾讯'").fetchone()["id"]

    def paid(*args, **kwargs):
        raise AssertionError("GET 不得安排付费任务")

    monkeypatch.setattr(routes, "schedule_digests", paid)
    monkeypatch.setattr(routes.thesis_map, "schedule", paid)
    monkeypatch.setattr("researchlib.views.schedule_views", paid)

    @contextmanager
    def readonly(request):
        connection = connect(settings)
        connection.execute("SET TRANSACTION READ ONLY")
        try:
            yield connection
        finally:
            connection.rollback()
            connection.close()

    monkeypatch.setattr(routes, "database", readonly)
    with TestClient(create_app(replace(settings, theses_auto_enabled=True))) as client:
        for params in ({}, {"author": "方伟"}, {"views": "risk"}, {"vpage": "2"}):
            response = client.get(f"/companies/{company}", params=params)
            assert response.status_code == 200


def test_unknown_identity_stays_visible_without_new_owner_claims(conn, settings):
    for source_id in ("fangwei-zsxq", "chengfengpolang"):
        row = _article(conn, settings, source_id)
        assert row["raw_meta"]["poster_role"] == "unknown"
        assert "poster_id" not in row["raw_meta"]
        with TestClient(create_app(settings)) as client:
            assert client.get(f"/articles/{row['id']}").status_code == 200
            assert "账户未核实" in client.get(f"/articles/{row['id']}").text
            assert row["title"] in client.get("/", params={"source": source_id}).text


@pytest.mark.parametrize("broken", ["model", "body", "title", "heading", "deleted"])
def test_stale_embeddings_do_not_rank_current_chunks(conn, settings, broken):
    row = _article(conn, settings)
    if embeddings.column_type(conn) is None:
        pytest.skip("需要 pgvector")
    embeddings.use_embedder(lambda texts: [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts], model="review-model")
    try:
        assert embeddings.backfill(conn) > 0
        kind = embeddings.column_type(conn)
        literal = embeddings.literal([1.0] + [0.0] * (embeddings.DIM - 1))
        scope = Scope(source_ids=["fangwei-mp"])
        assert _vector_keys(conn, kind, literal, scope, 1)
        if broken == "model":
            conn.execute("UPDATE chunk_embeddings SET model='obsolete-model'")
        elif broken == "title":
            conn.execute("UPDATE articles SET title=title || '新标题'")
        elif broken == "deleted":
            conn.execute("DELETE FROM article_chunks")
        else:
            conn.execute(f"UPDATE article_chunks SET {broken}={broken} || '已改变'")
        conn.commit()
        # ANN、范围窄时的穷举、现有关键词候选的逐块补分，都必须拒绝。
        for scoped in (scope, replace(scope, article_id=row["id"])):
            assert _vector_keys(conn, kind, literal, scoped, 1) == {}
        assert _vector_lookup(conn, kind, literal, scope, [{"article_id": row["id"], "ord": 0}], 1, []) == {}
    finally:
        embeddings.use_embedder(None)


def test_embedding_changed_during_model_call_is_deferred(conn, settings):
    _article(conn, settings)
    if embeddings.column_type(conn) is None:
        pytest.skip("需要 pgvector")
    calls = []

    def model(texts):
        calls.append(texts)
        conn.execute("UPDATE article_chunks SET body=body || '并发改动'")
        conn.commit()
        return [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts]

    embeddings.use_embedder(model, model="review-model")
    try:
        skipped = []
        assert embeddings.backfill(conn, batch=1, skipped=skipped) == 0
        assert skipped and len(calls) == 1
        assert conn.execute("SELECT count(*) AS n FROM chunk_embeddings").fetchone()["n"] == 0
    finally:
        embeddings.use_embedder(None)


def test_translation_job_holds_lock_across_internal_commits(conn, settings, monkeypatch):
    from researchlib.coordination import task
    from researchlib.translate import service
    conn.execute('SELECT pg_advisory_unlock_all()')
    other = connect(settings)
    try:
        def translate(connection, _article_id, **kwargs):
            connection.commit()
            assert not other.execute('SELECT pg_try_advisory_lock(%s) AS ok', (LOCK_KEY,)).fetchone()['ok']
            return 'completed'
        monkeypatch.setattr(service, '_translate_article', translate)
        assert service.translate_article(conn, 1, data_dir=settings.data_dir) == 'completed'
        assert other.execute('SELECT pg_try_advisory_lock(%s) AS ok', (LOCK_KEY,)).fetchone()['ok']
        with pytest.raises(DeploymentBusy), task(conn):
            raise AssertionError('独占锁期间不能启动新任务')
    finally:
        other.close()

"""学习重排只验证真实检索边界；给定测试分数不是模型效果证据。"""
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ranking.contracts import RerankScore
from researchlib.ranking.recall import collect_search_candidates
from researchlib.retrieval import reset_stats_cache, search_articles
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.queries import Scope


def _setup(conn, settings):
    sources = enabled_sources(load_sources(settings.sources_path))
    source = next(s for s in sources if s.id == "fangwei-mp")
    docs = [
        ("a", "设备机制甲", "阿斯麦的升级配件能够提高设备产出，这笔订单与整机销售分开收费。"),
        ("b", "设备机制乙", "ASML 为维护合同提供年度收费服务，装机越多，续签机会越多。"),
        ("foreign", "ASML 升级配件", "Walmart 的升级配件用于配送车辆，这里的费用属于沃尔玛。"),
    ]
    store_documents(conn, source=source, data_dir=settings.data_dir, archive_files=3, images_in_archive=0, errors=[],
                    docs=[ParsedDoc(k, t, datetime(2026, 1, 1, tzinfo=UTC), b, {}, []) for k, t, b in docs])
    conn.commit()
    reset_stats_cache()
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id,external_id FROM articles")}
    return sources, Scope(source_ids=[source.id]), ids


class GivenScores:
    model_id = "contract-only"
    model_revision = "fixture-v1"

    def __init__(self, preferred):
        self.preferred = str(preferred)
        self.seen = []

    def score(self, query, candidates):
        self.seen = list(candidates)
        return [RerankScore(c.candidate_id, float(c.article_id == self.preferred), c.window_hash) for c in candidates]


def test_reranking_changes_order_but_cannot_add_foreign_sources(conn, settings):
    sources, scope, ids = _setup(conn, settings)
    normal = search_articles(conn, "ASML 升级配件", scope, sources, use_semantic=False)
    assert normal.hits[0].article_id == ids["a"]
    model = GivenScores(ids["b"])
    changed = search_articles(conn, "ASML 升级配件", scope, sources, reranker=model, use_semantic=False)
    assert changed.hits[0].article_id == ids["b"]
    assert changed.ranking["status"] == "applied"
    assert str(ids["foreign"]) not in {c.article_id for c in model.seen}
    for candidate in model.seen:
        for fragment in candidate.fragments:
            body = conn.execute("SELECT body FROM article_chunks WHERE article_id=%s AND ord=%s",
                                (int(candidate.article_id), fragment.ord)).fetchone()["body"]
            assert body[fragment.start:fragment.end] == fragment.text
    assert changed.hits[0].passages[0].url.startswith(f"/articles/{ids['b']}?chunk=")


@pytest.mark.parametrize("bad", ["nan", "missing", "unknown", "hash", "duplicate"])
def test_invalid_model_output_falls_back_completely(conn, settings, bad):
    sources, scope, ids = _setup(conn, settings)
    baseline = search_articles(conn, "ASML 升级配件", scope, sources, use_semantic=False)
    class Broken(GivenScores):
        def score(self, query, candidates):
            output = super().score(query, candidates)
            if bad == "nan":
                return [RerankScore(c.candidate_id, float("nan"), c.window_hash) for c in candidates]
            if bad == "missing":
                return output[:-1]
            if bad == "unknown":
                return [RerankScore("missing-id", x.score, x.window_hash) for x in output]
            if bad == "hash":
                return [RerankScore(x.candidate_id, x.score, "old-window") for x in output]
            return [output[0]] * len(output)
    result = search_articles(conn, "ASML 升级配件", scope, sources, use_semantic=False, reranker=Broken(ids["b"]))
    assert result.ranking["status"] == "invalid_or_failed"
    assert [(h.article_id, h.score) for h in result.hits] == [(h.article_id, h.score) for h in baseline.hits]


def test_source_changed_during_model_call_is_not_returned_even_after_failure(conn, settings):
    sources, scope, ids = _setup(conn, settings)
    class Racing(GivenScores):
        def score(self, query, candidates):
            with psycopg.connect(settings.database_url) as writer:
                writer.execute(
                    "UPDATE article_chunks SET body='合成新版本，不是旧证据。' WHERE article_id=%s", (ids["a"],),
                )
            raise RuntimeError("fixture failure after source update")
    result = search_articles(conn, "ASML 升级配件", scope, sources, use_semantic=False, reranker=Racing(ids["a"]))
    assert ids["a"] not in {h.article_id for h in result.hits}
    assert result.ranking["dropped_stale"] >= 1


def test_candidate_export_uses_a_read_only_connection_and_no_embedding_call(conn, settings, monkeypatch):
    sources, scope, ids = _setup(conn, settings)
    monkeypatch.setattr("researchlib.retrieval.semantic_candidates",
                        lambda *args, **kwargs: pytest.fail("标注候选默认不能调用向量服务"))
    conn.commit()
    conn.read_only = True
    try:
        batch = collect_search_candidates(conn, "ASML 升级配件", scope, sources)
        assert batch.candidates and batch.query.entity_ids
        assert {c.source_id for c in batch.candidates} == {"fangwei-mp"}
        assert str(ids["foreign"]) not in {c.article_id for c in batch.candidates}
        assert all(c.source_version and c.document_family_id for c in batch.candidates)
    finally:
        conn.rollback()
        conn.read_only = False


def test_late_model_result_discards_entire_reranking(conn, settings, monkeypatch):
    sources, scope, ids = _setup(conn, settings)
    baseline = search_articles(conn, "ASML 升级配件", scope, sources, use_semantic=False)
    # 只测回传后的预算检查；同步Python调用不能靠此测试声称有硬中断超时。
    clock = iter((10.0, 11.0))
    monkeypatch.setattr("researchlib.ranking.runtime.time", SimpleNamespace(monotonic=lambda: next(clock)))
    result = search_articles(conn, "ASML 升级配件", scope, sources, use_semantic=False,
                             reranker=GivenScores(ids["b"]), rerank_budget_ms=250)
    assert result.ranking["status"] == "budget_exceeded"
    assert [(h.article_id, h.score) for h in result.hits] == [(h.article_id, h.score) for h in baseline.hits]

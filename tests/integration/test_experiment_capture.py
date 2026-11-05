"""真实本机 PostgreSQL 的只读捕获与原检索结果比对；全部原创合成资料。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import psycopg
import pytest
import yaml
from psycopg.types.json import Jsonb

from researchlib import embeddings
from researchlib.experiments.capture import capture
from researchlib.experiments.engine import compare
from researchlib.experiments.schema import CompareRequest, QuerySpec, Snapshot, Tuning
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import Budget, reset_stats_cache, retrieve, search_articles
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.queries import Scope


def seed(conn, settings):
    sources = enabled_sources(load_sources(settings.sources_path))
    first = next(s for s in sources if s.id == "fangwei-mp")
    second = next(s for s in sources if s.id == "gudujinhuati")
    for source, title, body in (
        (first, "会员制零售的经营机制", "会员制零售的收入分为商品和年费。稳定的续费让商家可以降低毛利。" * 14),
        (second, "会员制的现金流", "会员制零售关注续费率与客单价，现金流也需要覆盖门店租金。" * 17),
    ):
        store_documents(conn, source=source, docs=[ParsedDoc(
            external_id=f"experiment-{source.id}", title=title, published_at=datetime(2026, 1, 1, tzinfo=UTC),
            body_md=body, raw_meta={"format": "markdown", "poster_role": "owner"}, image_refs=[],
        )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    reset_stats_cache()
    return sources, first, second


def spec(ids, **kwargs):
    return QuerySpec(id="retail", query="会员制 零售", scope={"source_ids": ids, **kwargs})


def test_readonly_capture_matches_keyword_order_and_qa_material(conn, settings, monkeypatch):
    sources, first, second = seed(conn, settings)
    ids = [first.id, second.id]
    calls = []
    # 捕获绝不能因继承环境里配了 embedding 服务而调用它。
    monkeypatch.setattr(embeddings, "query_vector", lambda *_: calls.append("network-call"))
    before = conn.execute("SELECT count(*) AS n FROM llm_answers").fetchone()["n"]
    snapshot = capture(settings.database_url, settings.sources_path, [spec(ids)])
    assert calls == []
    replay = Snapshot.model_validate_json(snapshot.model_dump_json())
    result = compare(replay, CompareRequest(case_ids=["retail"], baseline=Tuning(), candidate=Tuning(title_bonus=1)))
    assert result["model_calls"] == 0
    normal = search_articles(conn, "会员制 零售", Scope(source_ids=ids), sources)
    assert result["cases"][0]["baseline"]["search"] == [
        {**asdict(h), "published_at": h.published_at.isoformat(), "passages": [
            {**asdict(p), "published_at": p.published_at.isoformat()} for p in h.passages
        ]} for h in normal.hits
    ]
    # 同样份量下，原 llm 检索和冻结重放使用同样的选段和前后文。
    import researchlib.retrieval as retrieval
    budgets = dict(retrieval.BUDGETS)
    budgets["llm"] = Budget(24, 24000, 3)
    monkeypatch.setattr(retrieval, "BUDGETS", budgets)
    found = retrieve(conn, "会员制 零售", Scope(source_ids=ids), sources)
    materials = result["cases"][0]["baseline"]["passages"]
    assert [(p.article_id, p.ord, p.body, p.before, p.after) for p in found.passages] == [
        (p["article_id"], p["ord"], p["text"], p["before"], p["after"]) for p in materials
    ]
    assert conn.execute("SELECT count(*) AS n FROM llm_answers").fetchone()["n"] == before


def test_capture_applies_source_article_date_hidden_and_duplicate_constraints(conn, settings, tmp_path):
    _, first, second = seed(conn, settings)
    article = conn.execute("SELECT id FROM articles WHERE source_id=%s", (first.id,)).fetchone()["id"]
    # 范围严格限定单篇、日期和来源；无关来源不进入上下文或邻段。
    snapshot = capture(settings.database_url, settings.sources_path, [spec(
        [first.id], article_id=article, date_from="2026-01-01", date_to="2026-01-02",
    )])
    rows = snapshot.cases[0].rows + snapshot.cases[0].qa_rows + snapshot.cases[0].neighbours
    assert rows and {r.article_id for r in rows} == {article} and {r.source_id for r in rows} == {first.id}
    conn.execute("UPDATE articles SET raw_meta=raw_meta || %s WHERE id=%s", (Jsonb({"hidden": "manual"}), article))
    conn.commit()
    hidden = capture(settings.database_url, settings.sources_path, [spec([first.id], article_id=article)])
    assert not hidden.cases[0].rows and not hidden.cases[0].qa_rows
    conn.execute("UPDATE articles SET raw_meta=raw_meta - 'hidden', duplicate_of=%s WHERE id=%s", (article, article))
    conn.commit()
    duplicate = capture(settings.database_url, settings.sources_path, [spec([first.id])])
    assert not duplicate.cases[0].rows
    # 来源停用后显式拒绝，不把来源参数丢掉退成全库。
    payload = yaml.safe_load(settings.sources_path.read_text())
    for source in payload["sources"]:
        if source["id"] == second.id:
            source["enabled"] = False
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True))
    with pytest.raises(ValueError, match="停用或未知"):
        capture(settings.database_url, path, [spec([second.id])])


def test_capture_database_enforces_readonly_even_if_a_helper_writes(conn, settings, monkeypatch):
    seed(conn, settings)
    import researchlib.experiments.capture as module
    def accidental_write(connection):
        connection.execute("UPDATE sources SET enabled=false")
    monkeypatch.setattr(module, "load_company_rows", accidental_write)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        capture(settings.database_url, settings.sources_path, [spec(["fangwei-mp"])])
    assert conn.execute("SELECT enabled FROM sources WHERE id='fangwei-mp'").fetchone()["enabled"]


def test_capture_rejects_no_terms_rather_than_pretending_to_compare(conn, settings):
    seed(conn, settings)
    question = spec(["fangwei-mp"]).model_copy(update={"query": "总结一下"})
    with pytest.raises(ValueError, match="明确检索词"):
        capture(settings.database_url, settings.sources_path, [question])


def test_capture_pins_real_localhost_connection_address(conn, settings, monkeypatch):
    _, first, _ = seed(conn, settings)
    from psycopg.conninfo import make_conninfo

    import researchlib.experiments.capture as module

    original = module.psycopg.connect
    connected = []
    def local_only_connect(**kwargs):
        assert kwargs["host"] == "localhost" and kwargs["hostaddr"] == "127.0.0.1"
        assert kwargs["port"].isdigit() and kwargs["dbname"] == "research_test"
        connection = original(**kwargs)
        connected.append(connection.pgconn.hostaddr.decode())
        return connection
    monkeypatch.setattr(module.psycopg, "connect", local_only_connect)
    snapshot = capture(
        make_conninfo(settings.database_url, host="localhost"), settings.sources_path, [spec([first.id])],
    )
    assert connected == ["127.0.0.1"] and snapshot.cases[0].rows

"""只从显式指定的本机实验库捕获快照；不读取 .env，不迁移，不配置向量/模型。"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from researchlib.experiments.schema import FrozenCase, QuerySpec, Row, Snapshot
from researchlib.retrieval import (
    _candidates,
    _ord_range,
    _rows_for_keys,
    analyze,
    load_company_rows,
    reset_stats_cache,
    score_context,
)
from researchlib.sources import enabled_sources, load_sources


def validate_dsn(dsn):
    params = conninfo_to_dict(dsn)
    # 不允许生产名称、远程主机、多个 host/hostaddr 或 libpq service 覆盖目标。
    if params.get("dbname") not in {"research_experiment", "research_eval", "research_test"}:
        raise ValueError("只能捕获 research_experiment / research_eval / research_test 离线副本")
    if params.get("host") not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("离线副本必须显式位于本机回环地址")
    if any(key in params for key in ("hostaddr", "service", "servicefile", "options")):
        raise ValueError("不接受 hostaddr/service/options 间接覆盖目标")
    return params


def capture(dsn: str, sources_path: Path, specs: list[QuerySpec]) -> Snapshot:
    params = validate_dsn(dsn)
    sources = enabled_sources(load_sources(sources_path))
    allowed = {s.id for s in sources}
    if not specs or len(specs) > 20 or len({s.id for s in specs}) != len(specs):
        raise ValueError("每个快照需 1～20 个不重复问题")
    for spec in specs:
        if not set(spec.scope.source_ids) <= allowed:
            raise ValueError("问题包含停用或未知来源，拒绝扩大范围")
    as_of = datetime.now(UTC)
    used_ids = {source for spec in specs for source in spec.scope.source_ids}
    # 此命令独立进程使用自己的缓存，不和主站共进程；从新库捕获前清空旧统计。
    reset_stats_cache()
    with psycopg.connect(**params, row_factory=dict_row, connect_timeout=5, autocommit=True) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            conn.execute("SET LOCAL statement_timeout = '20s'")
            conn.execute("SET LOCAL lock_timeout = '2s'")
            for statement in (
                "SET LOCAL zhparser.punctuation_ignore = on", "SET LOCAL zhparser.seg_with_duality = on",
                "SET LOCAL zhparser.multi_short = on",
            ):
                conn.execute(statement)
            if conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
                raise ValueError("快照连接未进入只读事务")
            # 双重确认实际目标，防止连接参数通过环境默认或代理选中另一库。
            if conn.execute("SELECT current_database() AS name").fetchone()["name"] != params["dbname"]:
                raise ValueError("连接目标与显式实验库不一致")
            permitted = {r["id"] for r in conn.execute(
                "SELECT id FROM sources WHERE enabled AND id = ANY(%s)", (sorted(used_ids),),
            ).fetchall()}
            if permitted != used_ids:
                raise ValueError("离线副本中的来源已停用或不存在，拒绝捕获")
            companies = load_company_rows(conn)
            cases = []
            for spec in specs:
                scope = spec.scope.scope()
                active = [s for s in sources if s.id in scope.source_ids]
                query = analyze(conn, spec.query, active, company_rows=companies)
                if query.authors:
                    ids = [s.id for s in active if s.author in query.authors]
                    if not ids:
                        raise ValueError("问题指定的作者不在授权来源中")
                    scope = replace(scope, source_ids=ids)
                    spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"source_ids": ids})})
                # 作者/概览退回按日列举不是关键词排序，避免把不同流程标成同一基线。
                if not query.terms or query.overview:
                    raise ValueError("第一版只接受含明确检索词的问题；请为概览问题补上主题词")
                qa_terms = [t for t in query.terms if t.kind != "word" or t.df > 0]
                qa_query = replace(query, terms=qa_terms, overview=not any(
                    t.kind in ("word", "phrase") for t in qa_terms
                ))
                if qa_query.overview:
                    raise ValueError("问库无有效主题词；请换一个能在副本中命中的问题")
                rows = _candidates(conn, query, scope, limit=2000, enough=2000)
                # 与主站 llm 的默认候选份量相同；两方案始终共用固定池，不按候选参数重新召回。
                qa_rows = _candidates(conn, qa_query, scope, limit=640, enough=480)
                wanted = set()
                for row in qa_rows:
                    low, high = _ord_range(row["ord"])
                    for ordinal in (row["ord"] - 1, row["ord"] + 1):
                        if low <= ordinal < high:
                            wanted.add((row["article_id"], ordinal))
                context = replace(score_context(conn, query, rows, scope, companies), as_of=as_of)
                qa_context = replace(score_context(conn, qa_query, qa_rows, scope, companies), as_of=as_of)
                keys = Row.model_fields.keys()
                def clean(items, keys=keys):
                    return [Row.model_validate({k: v for k, v in r.items() if k in keys}) for r in items]
                cases.append(FrozenCase(
                    spec=spec, query=query, qa_query=qa_query, rows=clean(rows), qa_rows=clean(qa_rows),
                    context=context, qa_context=qa_context, neighbours=clean(_rows_for_keys(conn, sorted(wanted))),
                ))
        finally:
            conn.execute("ROLLBACK")
    root = Path(__file__).resolve().parents[1]
    revision = hashlib.sha256((root / "retrieval.py").read_bytes()).hexdigest()
    return Snapshot(material="private_corpus", created_at=as_of, code_revision=revision,
                    sources=[s for s in sources if s.id in used_ids], cases=cases)

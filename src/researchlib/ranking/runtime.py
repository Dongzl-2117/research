"""有界、默认关闭的重排网关：只给已验证原文窗口打分，失败不借旧证据回退。"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict
from pathlib import Path

from researchlib import embeddings
from researchlib.chunks import OCR_ORD_BASE, TRANSLATION_ORD_BASE, current_chunk_sql
from researchlib.mentions import load_company_rows
from researchlib.ranking.contracts import SCHEMA_VERSION, RankCandidate, RankQuery, SourceFragment, digest
from researchlib.web.queries import scope_clause

MAX_RERANK_CANDIDATES = 100


def query_snapshot(query, scope) -> RankQuery:
    scope_json = json.dumps(asdict(scope), default=str, ensure_ascii=False, sort_keys=True)
    entities = tuple(str(c["id"]) for c in query.companies)
    if not entities and scope.company_id is not None:
        entities = (str(scope.company_id),)
    return RankQuery(
        digest([query.raw, scope_json, SCHEMA_VERSION]), query.raw, query.topic_text, entities, scope_json,
    )


def _rows(conn, scope, passages):
    keys = set()
    for p in passages:
        low, high = (OCR_ORD_BASE, 10**9) if p.ord >= OCR_ORD_BASE else (
            (TRANSLATION_ORD_BASE, OCR_ORD_BASE) if p.ord >= TRANSLATION_ORD_BASE else (0, TRANSLATION_ORD_BASE)
        )
        keys.update((p.article_id, n) for n in (p.ord - 1, p.ord, p.ord + 1) if low <= n < high)
    if not keys:
        return {}
    where, params = scope_clause(scope)
    params.update(ids=[k[0] for k in keys], ords=[k[1] for k in keys])
    rows = conn.execute(
        f"""SELECT a.id AS article_id, a.source_id, a.content_hash, a.title AS source_title,
            coalesce(a.duplicate_of, a.id) AS family, a.published_at,
            c.ord, c.heading, c.body, c.lang, c.anchor,
            CASE WHEN c.lang = 'zh' THEN coalesce(nullif(tr.title_zh, ''), a.title) ELSE a.title END AS title,
            CASE WHEN c.lang = 'zh'
              THEN concat_ws(':',tr.content_hash,tr.updated_at::text,tr.title_zh) END AS translation
            FROM unnest(%(ids)s::bigint[], %(ords)s::int[]) AS k(article_id, ord)
            JOIN articles a ON a.id = k.article_id
            JOIN article_chunks c ON c.article_id = a.id AND c.ord = k.ord
            LEFT JOIN article_translations tr ON tr.article_id = a.id AND tr.lang = 'zh'
              AND tr.content_hash = a.content_hash
            WHERE {where} AND {current_chunk_sql()}""", params,
    ).fetchall()
    return {(r["article_id"], r["ord"]): r for r in rows}


def snapshot_candidates(conn, rank_query, scope, passages, recall_trace):
    rows = _rows(conn, scope, passages)
    result, originals = [], {}
    for p in passages:
        center = rows.get((p.article_id, p.ord))
        if (not center or p.body != center["body"] or p.heading != center["heading"] or p.title != center["title"]
                or (rank_query.entity_ids and not set(rank_query.entity_ids) & set(p.entity_ids))):
            continue
        version = digest([center["content_hash"], center["source_title"], center["translation"]])
        fragments = []
        for role, text, ords in (
            ("center", p.excerpt or p.body, (p.ord,)),
            ("before", p.before, (p.ord - 1, p.ord)),
            ("after", p.after, (p.ord + 1, p.ord)),
        ):
            if not text:
                continue
            fragment = None
            for ord_ in ords:
                row = rows.get((p.article_id, ord_))
                if not row or row["lang"] != center["lang"]:
                    continue
                start = row["body"].find(text)
                if start < 0:
                    continue
                fragment = SourceFragment(
                    role, ord_, start, start + len(text), text,
                    digest([version, row["heading"], row["body"], row["lang"], row["anchor"]]),
                    row["lang"], row["anchor"],
                )
                break
            if fragment is None:
                break
            fragments.append(fragment)
        else:
            candidate = RankCandidate(
                query_id=rank_query.query_id, source_id=p.source_id, article_id=str(p.article_id),
                document_family_id=f"article:{center['family']}", source_version=version,
                title=p.title, heading=p.heading, fragments=tuple(fragments), entity_ids=p.entity_ids,
                entity_basis=p.entity_basis, url=p.url, published_at=str(p.published_at or ""),
                recall_channels=tuple(sorted(recall_trace.get((p.article_id, p.ord), {"context"}))),
                lexical_score=p.topic_lexical if "lexical" in recall_trace.get((p.article_id, p.ord), ()) else None,
                semantic_score=p.semantic if "semantic" in recall_trace.get((p.article_id, p.ord), ()) else None,
                semantic_model=embeddings.model_name()
                if "semantic" in recall_trace.get((p.article_id, p.ord), ()) else None,
            )
            RankCandidate.from_record(candidate.to_record())
            result.append(candidate)
            originals[candidate.candidate_id] = p
    return tuple(result), originals


def _balanced_pool(candidates, originals, limit):
    """保留词法、语义和公司上下文三路机会，不把旧手写总分当成学习模型。"""
    queues = [
        sorted((c for c in candidates if c.lexical_score is not None), key=lambda c: -c.lexical_score),
        sorted((c for c in candidates if c.semantic_score is not None), key=lambda c: -c.semantic_score),
        sorted(candidates, key=lambda c: -originals[c.candidate_id].score),
    ]
    pool, seen = [], set()
    while len(pool) < limit and any(queues):
        for queue in queues:
            while queue and queue[0].candidate_id in seen:
                queue.pop(0)
            if queue and len(pool) < limit:
                candidate = queue.pop(0)
                seen.add(candidate.candidate_id)
                pool.append(candidate)
    return tuple(pool)


def rerank_windows(
    conn, query, scope, windows, recall_trace, *, reranker=None, observer=None,
    limit=80, budget_ms=250.0,
):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RERANK_CANDIDATES:
        raise ValueError("重排候选份量必须在1到100之间")
    if (isinstance(budget_ms, bool) or not isinstance(budget_ms, (float, int))
            or not math.isfinite(budget_ms) or budget_ms <= 0):
        raise ValueError("重排预算必须是正有限数")
    rank_query = query_snapshot(query, scope)
    entity_version = digest(load_company_rows(conn))
    candidates, originals = snapshot_candidates(conn, rank_query, scope, windows, recall_trace)
    pool = _balanced_pool(candidates, originals, limit)
    trace = {"status": "disabled", "candidates": len(candidates), "model_candidates": len(pool),
             "model_id": None, "model_revision": None, "elapsed_ms": 0.0, "dropped_stale": 0}
    scores = None
    if observer is not None:
        observer(rank_query, pool)
    if reranker is not None and pool:
        started = time.monotonic()
        try:
            trace.update(model_id=reranker.model_id, model_revision=reranker.model_revision)
            if not all(isinstance(trace[k], str) and trace[k] for k in ("model_id", "model_revision")):
                raise ValueError("缺少模型版本")
            output = reranker.score(rank_query, pool)
            if not isinstance(output, (list, tuple)) or len(output) != len(pool):
                raise ValueError("重排器输出份量不符")
            expected = {c.candidate_id: c.window_hash for c in pool}
            scores = {}
            for item in output:
                if (item.candidate_id not in expected or item.candidate_id in scores
                        or item.window_hash != expected[item.candidate_id]
                        or isinstance(item.score, bool) or not isinstance(item.score, (float, int))
                        or not math.isfinite(item.score)):
                    raise ValueError("重排器返回了无效ID、窗口版本或分数")
                scores[item.candidate_id] = float(item.score)
            if set(scores) != set(expected):
                raise ValueError("重排器未给出完整候选分数")
            trace["status"] = "applied"
        except Exception:
            # 不暴露模型异常中的私有原文或连接参数；完整回退，不把半批结果混入排序。
            scores = None
            trace["status"] = "invalid_or_failed"
        trace["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        if trace["elapsed_ms"] > budget_ms:
            scores = None
            trace["status"] = "budget_exceeded"
    # 回调也可能耗时/并发改库。即使模型失败，仍不能把过期原文带回结果。
    current, _ = snapshot_candidates(conn, rank_query, scope, windows, recall_trace)
    valid = {c.candidate_id for c in current}
    if digest(load_company_rows(conn)) != entity_version:
        valid = set()
        trace["status"] = "entity_rules_changed"
    trace["dropped_stale"] = len(candidates) - len(valid)
    trace["eligible_candidate_ids"] = sorted(c.candidate_id for c in pool if c.candidate_id in valid)
    ordered = sorted((c for c in candidates if c.candidate_id in valid),
                     key=lambda c: -originals[c.candidate_id].score)
    if scores is not None:
        ranked = sorted((c for c in pool if c.candidate_id in valid), key=lambda c: -scores[c.candidate_id])
        ranked_ids = {c.candidate_id for c in ranked}
        ordered = ranked + [c for c in ordered if c.candidate_id not in ranked_ids]
        # 学习分数可为负，转换为单调正序号以兼容现有窗口聚合；不伪装成概率。
        for index, candidate in enumerate(ordered):
            originals[candidate.candidate_id].score = float(len(ordered) - index)
    return [originals[c.candidate_id] for c in ordered], trace


def load_reviewed_ranker(model_path: Path | None = None, review_path: Path | None = None):
    """默认关闭；校验精确版本与审阅声明，实际人工审阅和独立评测不能由 JSON 自证。"""
    if model_path is None:
        return None, {"status": "disabled"}
    if review_path is None:
        return None, {"status": "review_required"}

    def review_object(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ValueError("审阅 JSON 有重复字段")
            record[key] = value
        return record

    def reject_constant(_value):
        raise ValueError("审阅 JSON 不允许非有限数字")

    try:
        from researchlib.ranking.linear import LinearRanker
        if model_path.stat().st_size > 2_000_000 or review_path.stat().st_size > 64_000:
            raise ValueError("模型或审阅文件过大")
        model = LinearRanker.from_json(model_path.read_text(encoding="utf-8"))
        review = json.loads(review_path.read_text(encoding="utf-8"),
                            object_pairs_hook=review_object, parse_constant=reject_constant)
        identity_fields = ("reviewer_id", "validation_manifest_id", "test_manifest_id")
        if not isinstance(review, dict) or any(
            not isinstance(review.get(key), str) or not review[key].strip() for key in identity_fields
        ):
            raise ValueError("审阅者和评测清单必须是非空白字符串")
        if (review.get("approved") is not True or review.get("model_revision") != model.model_revision
                or review.get("training_only") is not False
                or not dict(model.summary.used_material_counts)["private_corpus"]):
            raise ValueError("模型没有符合要求的独立审阅或仅使用合成材料")
        return model, {"status": "loaded", "model_id": model.model_id, "model_revision": model.model_revision}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None, {"status": "rejected"}

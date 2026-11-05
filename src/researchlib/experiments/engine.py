"""共用主站词法评分和文章聚合，冻结候选上回放；不导入业务数据库或模型配置。"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from researchlib.ask import SYSTEM_PROMPT, _passage_dict, build_messages
from researchlib.experiments.schema import VERSION, CompareRequest, Snapshot, Tuning, digest, now
from researchlib.retrieval import Budget, _ord_range, rank_articles, score_rows, select


def implementation_sha() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = [root / name for name in (
        "retrieval.py", "ask.py", "experiments/engine.py", "experiments/schema.py", "experiments/site_model.py",
    )]
    return hashlib.sha256(b"\n".join(p.read_bytes() for p in paths)).hexdigest()


def run_id(snapshot: Snapshot, request: CompareRequest, *, generation_profile=None):
    return digest({"snapshot": snapshot.fingerprint, "request": request.model_dump(),
                   "engine": implementation_sha(), "generation_profile": generation_profile})


def _context(picked, neighbours, extra):
    chosen = {(p.article_id, p.ord) for p in picked}
    bodies = {(r.article_id, r.ord): r.body for r in neighbours}
    taken = set()
    used = 0
    for p in picked:
        low, high = _ord_range(p.ord)
        for ordinal, side in ((p.ord - 1, "before"), (p.ord + 1, "after")):
            key = p.article_id, ordinal
            body = bodies.get(key, "")
            if not low <= ordinal < high or key in chosen or key in taken or used + len(body) > extra:
                continue
            setattr(p, side, body)
            used += len(body)
            taken.add(key)


def variant(case, tuning: Tuning, sources):
    started = perf_counter()
    scored = score_rows(
        None, case.query, [r.model_dump() for r in case.rows], scope=case.spec.scope.scope(),
        context=case.context, parameters=tuning.scoring(),
    )
    hits = rank_articles(scored, case.query, case.context.company_rows)[:20]
    search_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    scored = score_rows(
        None, case.qa_query, [r.model_dump() for r in case.qa_rows], scope=case.spec.scope.scope(),
        context=case.qa_context, parameters=tuning.scoring(),
    )
    budget = Budget(tuning.passages, tuning.chars, tuning.per_article)
    # 主站允许第一段超预算以保留完整原文；单次实验先剔除放不下的整段，
    # 再交给 select 分配段数/字符预算，避免大段先占名额、事后移除又丢掉可用的小段。
    fitting = [p for p in scored if len(p.body) <= tuning.chars]
    bounded = select(fitting, case.qa_query, budget, sources, min_score_ratio=tuning.min_score_ratio)
    _context(bounded, case.neighbours, int(tuning.chars * 0.3))
    passages = [_passage_dict(i, p, sources, "Asia/Shanghai") for i, p in enumerate(bounded, 1)]
    system = SYSTEM_PROMPT + ("\n\n实验附加要求：\n" + tuning.prompt_note if tuning.prompt_note else "")
    messages = build_messages(case.spec.query, passages, system=system)
    # 标题/元数据也算进模型材料上限，超过时只导出，不允许模型调用。
    return {
        "search": [asdict(h) for h in hits], "passages": passages, "messages": messages,
        "generation_eligible": bool(passages) and sum(len(m["content"]) for m in messages) <= 40000,
        "metrics": {
            "search_ms": round(search_ms, 3), "retrieval_ms": round((perf_counter() - started) * 1000, 3),
            "articles": len({p.article_id for p in bounded}), "sources": len({p.source_id for p in bounded}),
            "passages": len(bounded),
            "material_chars": sum(len(p.body) + len(p.before) + len(p.after) for p in bounded),
        },
    }


def compare(snapshot: Snapshot, request: CompareRequest, *, generation_profile=None):
    by_id = {case.spec.id: case for case in snapshot.cases}
    if not set(request.case_ids) <= by_id.keys():
        raise ValueError("问题不在当前快照中")
    sources = {s.id: s for s in snapshot.sources}
    results = []
    for case_id in request.case_ids:
        case = by_id[case_id]
        sides = {name: variant(case, getattr(request, name), sources) for name in ("baseline", "candidate")}
        a, b = [{h["article_id"] for h in sides[side]["search"][:10]} for side in ("baseline", "candidate")]
        results.append({
            "id": case_id, "query": case.spec.query, "scope": case.spec.scope.model_dump(mode="json"),
            "search_overlap_at_10": len(a & b) / len(a | b) if a | b else None, **sides,
        })
    # datetime 交给统一 JSON 序列化；不把时间/耗时伪装为评分提升。
    import json
    payload = {
        "id": run_id(snapshot, request, generation_profile=generation_profile),
        "schema_version": 1, "recipe": VERSION, "generation_profile": generation_profile,
        "snapshot_id": snapshot.fingerprint, "implementation_sha": implementation_sha(),
        "created_at": now(), "material": snapshot.material, "request": request.model_dump(),
        "cases": results, "model_calls": 0, "cost": None,
    }
    return json.loads(json.dumps(payload, ensure_ascii=False, default=lambda x: x.isoformat()))

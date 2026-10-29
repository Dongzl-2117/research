"""通用词法/语义候选提供者接口与只读检索快照入口。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

from researchlib.ranking.contracts import RankCandidate, RankQuery


@dataclass(frozen=True)
class CandidateBatch:
    query: RankQuery
    candidates: tuple[RankCandidate, ...]
    trace: dict


def collect_hybrid(query, providers, *, eligible, limit=80):
    """以RRF合并可替换提供者；硬约束由调用方明确提供，模型分数不能绕过。"""
    if not 1 <= limit <= 100:
        raise ValueError("候选份量必须在1到100之间")
    scores, candidates, channels = defaultdict(float), {}, defaultdict(set)
    for provider in providers:
        seen = set()
        for rank, candidate in enumerate(provider.collect(query, limit=limit), 1):
            RankCandidate.from_record(candidate.to_record())
            if candidate.query_id != query.query_id or not eligible(query, candidate):
                continue
            if candidate.candidate_id in seen:
                continue
            seen.add(candidate.candidate_id)
            old = candidates.setdefault(candidate.candidate_id, candidate)
            if old.window_hash != candidate.window_hash:
                raise ValueError("同一候选ID对应不同原文版本")
            channels[candidate.candidate_id].add(provider.name)
            scores[candidate.candidate_id] += 1 / (60 + rank)
    ordered = sorted(candidates, key=lambda cid: (-scores[cid], cid))[:limit]
    return tuple(replace(candidates[cid], recall_channels=tuple(sorted(channels[cid]))) for cid in ordered)


def collect_search_candidates(conn, text, scope, sources, *, limit=80, use_semantic=False):
    """只读现有检索：词法、可选已有语义和公司上下文，经局部实体/版本约束后导出。

    标注默认不调用向量服务；use_semantic需操作者显式选择现有服务，无模型不伪造语义分数。
    不打开业务库、不排模型任务，连接与来源权限由调用方提供。
    """
    from researchlib.retrieval import search_articles
    snapshots = []
    result = search_articles(
        conn, text, scope, sources, use_semantic=use_semantic, rerank_limit=limit,
        ranking_observer=lambda query, candidates: snapshots.append((query, candidates)),
    )
    if not snapshots:
        raise ValueError("查询没有可导出的候选主题")
    query, candidates = snapshots[0]
    return CandidateBatch(query, candidates, result.ranking or {})

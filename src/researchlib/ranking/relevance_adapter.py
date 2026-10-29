"""PR115 标注 schema 的独立适配器；使用其权威校验，不维护第二套 JSONL 格式。"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from researchlib.ranking.contracts import RankCandidate, RankQuery, SourceFragment, digest
from researchlib.ranking.groupaudit import audit_groups
from researchlib.ranking.linear import PairwiseExample


def _schema():
    try:
        from researchlib.relevance import schema, store
    except ModuleNotFoundError as exc:
        raise RuntimeError("需要 PR115 的 researchlib.relevance schema；请在独立组合环境校验，不复制模块") from exc
    return schema, store


@dataclass(frozen=True)
class LabelledPair:
    task_id: str
    split: str
    group_id: str
    event_id: str
    labeler_id: str
    decision: str
    revision: int
    example: PairwiseExample


@dataclass(frozen=True)
class PreferenceData:
    manifest_id: str
    pairs: tuple[LabelledPair, ...]
    report: dict

    def examples(self, split):
        if split not in {"train", "validation", "test"}:
            raise ValueError("未知冻结划分")
        return tuple(p.example for p in self.pairs if p.split == split)


def _candidate(candidate, query):
    fragments = tuple(
        SourceFragment(role, -1, None, None, text, "", coordinates="window",
                       external_chunk_id=candidate.chunk_id)
        for role, text in (("center", candidate.text), ("before", candidate.context_before),
                           ("after", candidate.context_after)) if text
    )
    return RankCandidate(
        query_id=query.query_id, source_id=candidate.source_id, article_id=candidate.article_id,
        document_family_id=candidate.document_family_id, source_version=candidate.content_version,
        title=candidate.article_title, heading=candidate.heading, fragments=fragments,
        entity_ids=query.entity_ids, entity_basis="annotation-task-scope-not-live-proof", url=candidate.source_url,
        recall_channels=(candidate.algorithm.name,), opaque_id=candidate.candidate_id,
    )


def adapt_records(records) -> PreferenceData:
    """保留最新事件；左右仅是展示位置，winner_id 才是候选身份。撤回不进入训练。"""
    _, store = _schema()
    dataset = store.validate_records(records)
    grouping = audit_groups([f.model_dump(mode="json") for f in dataset.tasks])
    latest = {}
    for event in dataset.events:
        key = (event.task_id, event.source.kind, event.source.labeler_id)
        if key not in latest or event.revision > latest[key].revision:
            latest[key] = event
    tasks = {f.task.task_id: f for f in dataset.tasks}
    pairs, decisions, sources, materials = [], Counter(), Counter(), Counter()
    human_votes = {}
    for key in sorted(latest):
        event = latest[key]
        frozen = tasks[event.task_id]
        task = frozen.task
        decisions[event.decision] += 1
        sources[event.source.kind] += 1
        materials[task.material] += 1
        if event.source.kind == "human" and event.decision in {"left", "right"}:
            human_votes.setdefault(task.task_id, set()).add(event.winner_id)
        if event.decision == "withdraw":
            continue
        query = RankQuery(task.query_id, task.query, task.query, tuple(task.entity_ids),
                          json.dumps(task.scope, ensure_ascii=False, sort_keys=True),
                          parser_version="relevance-schema-v1", topic_group_id=digest(task.topic_ids))
        a, b = (_candidate(c, query) for c in task.candidates)
        if event.decision in {"left", "right"}:
            choice = "a_better" if event.winner_id == a.candidate_id else "b_better"
        else:
            choice = {"tie": "tie", "neither": "both_irrelevant", "skip": "cannot_judge",
                      "uncertain": "cannot_judge"}[event.decision]
        pairs.append(LabelledPair(
            task.task_id, frozen.split, frozen.group_id, event.event_id, event.source.labeler_id,
            event.decision, event.revision,
            PairwiseExample(query, a, b, choice, "user" if event.source.kind == "human" else "ai", task.material),
        ))
    return PreferenceData(dataset.manifest.manifest_id, tuple(pairs), {
        "ui_coverage": dataset.report(), "group_audit": grouping,
        "history_events": len(dataset.events), "latest_events": len(latest),
        "latest_decisions": dict(sorted(decisions.items())), "latest_sources": dict(sorted(sources.items())),
        "latest_materials": dict(sorted(materials.items())),
        "human_direction_disagreement_tasks": sum(len(votes) > 1 for votes in human_votes.values()),
        "limitations": ["任务实体是标注范围，不是对候选每句归属的重新证明；线上仍须经过实时硬约束。",
                        "历史 UI 窗口没有原块偏移；保留窗口坐标 unknown，不生成原文坐标。",
                        "相当、都不相关、跳过、无法判断分别统计；跳过/无法判断不训练，撤回被排除。"],
    })


def read_preferences(path: Path) -> PreferenceData:
    _, store = _schema()
    return adapt_records(store.read_jsonl(path))


def _scope(value, enabled_ids):
    from researchlib.web.queries import Scope
    allowed = {"source_ids", "company_id", "date_from", "date_to", "article_id", "tree_id"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("标注范围包含未知字段")
    fields = dict(value)
    ids = fields.get("source_ids", list(enabled_ids))
    if (not isinstance(ids, list) or not ids or any(not isinstance(s, str) for s in ids)
            or set(ids) - set(enabled_ids)):
        raise ValueError("标注来源必须为显式允许且启用的来源")
    fields["source_ids"] = ids
    for name in ("company_id", "article_id", "tree_id"):
        v = fields.get(name)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
            raise ValueError(f"{name} 必须为正整数")
    for name in ("date_from", "date_to"):
        if fields.get(name) is not None:
            fields[name] = date.fromisoformat(fields[name])
    if fields.get("date_from") and fields.get("date_to") and fields["date_from"] > fields["date_to"]:
        raise ValueError("日期范围倒置")
    return Scope(**fields)


def build_library_provider(*, connection_factory, sources, public_base_url, topic_resolver,
                           material="private_corpus", limit=80):
    """显式选配到 PR115 本机服务的 provider。每次新连接，仅只读，不 prepare/迁移/调用模型。

    topic_resolver 必须提供经维护的主题族，不能用 query 哈希冒充未见主题。工厂和允许来源由
    操作员配置，不读取默认 DATABASE_URL。首次部署前应由维护者确认连接目标与只读角色。
    """
    parsed = urlsplit(public_base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("必须配置无凭据的原站 HTTP(S) 基址")
    if material not in {"private_corpus", "synthetic"} or not callable(topic_resolver):
        raise ValueError("必须明确语料类型和主题族解析器")
    allowed_sources = tuple(s for s in sources if s.enabled)
    enabled_ids = tuple(s.id for s in allowed_sources)

    def provider(*, query: str, scope: dict):
        schema, _ = _schema()
        from researchlib.db import ensure_search_session
        from researchlib.ranking.recall import collect_search_candidates
        text = " ".join(query.split())
        if not text or len(text) > 500:
            raise ValueError("标注查询必须为1到500字，不静默截断")
        selected_scope = _scope(scope, enabled_ids)
        topics = topic_resolver(query=text, scope=scope)
        if not isinstance(topics, list) or not topics or any(not isinstance(t, str) or not t.strip() for t in topics):
            raise ValueError("缺少明确主题族，拒绝生成无法防主题泄漏的任务")
        with connection_factory() as conn:
            conn.read_only = True
            ensure_search_session(conn)
            if conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
                raise ValueError("候选连接不是只读事务")
            batch = collect_search_candidates(conn, text, selected_scope, allowed_sources, limit=limit,
                                              use_semantic=False)
        chosen, seen = [], set()
        for candidate in batch.candidates:
            # 两段同文相邻窗通常没有足够比较价值；至少保留不同窗口文字。
            if candidate.window_hash in seen:
                continue
            seen.add(candidate.window_hash)
            center = next(f for f in candidate.fragments if f.role == "center")
            chosen.append(schema.Candidate(
                source_id=candidate.source_id, article_id=candidate.article_id,
                chunk_id=f"{candidate.article_id}:{center.ord}", window_id=candidate.window_hash,
                document_family_id=candidate.document_family_id, content_version=candidate.source_version,
                article_title=candidate.title, heading=candidate.heading, text=candidate.text,
                context_before=candidate.before, context_after=candidate.after,
                source_url=urljoin(public_base_url.rstrip("/") + "/", candidate.url),
                algorithm=schema.Algorithm(name="research-current-lexical-context", version="ranking-v1"),
            ))
            if len(chosen) == 2:
                break
        if len(chosen) < 2:
            raise ValueError("当前范围不足两段独立候选，未制造替代答案")
        return schema.Task(query=text, scope=scope, entity_ids=list(batch.query.entity_ids),
                           topic_ids=topics, candidates=tuple(chosen), material=material)

    return provider

"""通用排序与偏好标注的 v1 快照契约；只包含不可变原文及分数，不允许模型改写引用。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol

SCHEMA_VERSION = "research-ranking-v1"
CHOICES = frozenset({"a_better", "b_better", "tie", "both_irrelevant", "cannot_judge"})
LABEL_SOURCES = frozenset({"user", "ai", "synthetic"})


def digest(value) -> str:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class RankQuery:
    query_id: str
    text: str
    topic_text: str
    entity_ids: tuple[str, ...]
    scope_json: str
    parser_version: str = SCHEMA_VERSION
    topic_group_id: str | None = None

    @property
    def scope_hash(self) -> str:
        return digest(json.loads(self.scope_json))

    def to_record(self) -> dict:
        record = asdict(self)
        record["scope"] = json.loads(record.pop("scope_json"))
        record["scope_hash"] = self.scope_hash
        record["entity_ids"] = list(self.entity_ids)
        return record

    @classmethod
    def from_record(cls, record: dict) -> RankQuery:
        query = cls(
            record["query_id"], record["text"], record["topic_text"], tuple(record["entity_ids"]),
            json.dumps(record["scope"], ensure_ascii=False, sort_keys=True),
            record.get("parser_version", SCHEMA_VERSION), record.get("topic_group_id"),
        )
        if record.get("scope_hash", query.scope_hash) != query.scope_hash:
            raise ValueError("查询范围快照哈希不符")
        return query


@dataclass(frozen=True)
class SourceFragment:
    role: str  # center | before | after
    ord: int
    start: int
    end: int
    text: str
    chunk_version: str
    lang: str = ""
    anchor: str = ""


@dataclass(frozen=True)
class RankCandidate:
    query_id: str
    source_id: str
    article_id: str
    document_family_id: str
    source_version: str
    title: str
    heading: str
    fragments: tuple[SourceFragment, ...]
    entity_ids: tuple[str, ...]
    entity_basis: str
    url: str
    published_at: str = ""
    recall_channels: tuple[str, ...] = ()
    lexical_score: float | None = None
    semantic_score: float | None = None
    semantic_model: str | None = None

    def fragment_text(self, role: str) -> str:
        return "\n".join(f.text for f in self.fragments if f.role == role)

    @property
    def text(self) -> str:
        return self.fragment_text("center")

    @property
    def before(self) -> str:
        return self.fragment_text("before")

    @property
    def after(self) -> str:
        return self.fragment_text("after")

    @property
    def window_hash(self) -> str:
        return digest({"source_version": self.source_version, "title": self.title, "heading": self.heading,
                       "fragments": [asdict(f) for f in self.fragments]})

    @property
    def candidate_id(self) -> str:
        return digest([self.query_id, self.source_id, self.article_id, self.window_hash, list(self.entity_ids)])

    def to_record(self) -> dict:
        record = asdict(self)
        record.update(candidate_id=self.candidate_id, window_hash=self.window_hash,
                      text=self.text, before=self.before, after=self.after)
        record["fragments"] = [asdict(f) for f in self.fragments]
        record["entity_ids"] = list(self.entity_ids)
        record["recall_channels"] = list(self.recall_channels)
        return record

    @classmethod
    def from_record(cls, record: dict) -> RankCandidate:
        fields = dict(record)
        for name in ("candidate_id", "window_hash", "text", "before", "after"):
            fields.pop(name, None)
        fields["fragments"] = tuple(SourceFragment(**f) for f in record["fragments"])
        fields["entity_ids"] = tuple(record["entity_ids"])
        fields["recall_channels"] = tuple(record.get("recall_channels", []))
        candidate = cls(**fields)
        if sum(f.role == "center" for f in candidate.fragments) != 1:
            raise ValueError("每个候选必须有一个中心原文片段")
        for f in candidate.fragments:
            if f.role not in {"center", "before", "after"} or not 0 <= f.start <= f.end:
                raise ValueError("原文片段范围或角色无效")
            if f.end - f.start != len(f.text) or not f.chunk_version:
                raise ValueError("原文片段长度或版本无效")
        for key in ("candidate_id", "window_hash", "text", "before", "after"):
            if key in record and record[key] != getattr(candidate, key):
                raise ValueError(f"候选快照 {key} 不符")
        return candidate


@dataclass(frozen=True)
class RerankScore:
    candidate_id: str
    score: float
    window_hash: str


class RecallProvider(Protocol):
    """词法、语义等候选提供者；合并后仍须经实体/范围/版本硬约束。"""

    name: str

    def collect(self, query: RankQuery, *, limit: int) -> list[RankCandidate]: ...


class Reranker(Protocol):
    """每个分数必须同时基于当前 query 和该 candidate。分数不是概率或“已回答”声明。"""

    model_id: str
    model_revision: str

    def score(self, query: RankQuery, candidates: tuple[RankCandidate, ...]) -> list[RerankScore]: ...

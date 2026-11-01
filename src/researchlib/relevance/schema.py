"""可回放的 JSONL 契约。ID 由实际展示证据计算，算法分数不进入标签含义。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

SCHEMA_VERSION = 1
GROUPING_VERSION = "entity-topic-family-neardup-v1"
Decision = Literal["left", "right", "tie", "neither", "skip", "uncertain", "withdraw"]
Split = Literal["train", "validation", "test"]


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalized_query(query: str) -> str:
    return " ".join(query.split())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Algorithm(Model):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)
    model_name: str | None = Field(default=None, max_length=200)
    model_version: str | None = Field(default=None, max_length=200)


class Candidate(Model):
    source_id: str = Field(default="unknown", min_length=1, max_length=300)
    article_id: str = Field(min_length=1, max_length=300)
    chunk_id: str = Field(min_length=1, max_length=300)
    window_id: str = Field(min_length=1, max_length=300)
    document_family_id: str = Field(min_length=1, max_length=300)
    content_version: str = Field(min_length=1, max_length=300)
    article_title: str = Field(min_length=1, max_length=1000)
    heading: str = Field(default="", max_length=1000)
    text: str = Field(min_length=1, max_length=30000)
    context_before: str = Field(default="", max_length=30000)
    context_after: str = Field(default="", max_length=30000)
    source_url: str = Field(min_length=1, max_length=4000)
    algorithm: Algorithm
    content_hash: str = ""
    candidate_id: str = ""

    @field_validator("source_id", mode="before")
    @classmethod
    def source_identity(cls, value):
        # 历史包缺失来源时保留未知，不能从范围或候选算法猜单篇来源。
        return "unknown" if value is None or value == "" else value

    @field_validator("source_url")
    @classmethod
    def safe_url(cls, value):
        # 只放行 HTTP(S) 或本站合成来源页，防止导入的 URL 执行脚本、泄露本机文件。
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
            return value
        if re.fullmatch(r"/annotate/source/[a-f0-9]{64}", value):
            return value
        raise ValueError("来源必须为 HTTP(S) 地址或本站合成来源页")

    @model_validator(mode="after")
    def identities(self):
        content_hash = digest(
            {key: getattr(self, key) for key in ("article_title", "heading", "text", "context_before", "context_after")}
        )
        identity = digest(
            {key: getattr(self, key) for key in ("article_id", "chunk_id", "window_id", "content_version")}
            | {"content_hash": content_hash}
        )
        if self.content_hash and self.content_hash != content_hash:
            raise ValueError("候选 content_hash 与展示内容不符")
        if self.candidate_id and self.candidate_id != identity:
            raise ValueError("candidate_id 与稳定 ID/内容版本不符")
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "candidate_id", identity)
        return self


class Task(Model):
    query: str = Field(min_length=1, max_length=3000)
    scope: dict[str, JsonValue] = Field(default_factory=dict)
    entity_ids: list[str] = Field(max_length=100)
    topic_ids: list[str] = Field(min_length=1, max_length=100)
    candidates: tuple[Candidate, Candidate]
    material: Literal["synthetic", "private_corpus"]
    query_id: str = ""
    task_id: str = ""

    @field_validator("entity_ids", "topic_ids")
    @classmethod
    def group_names(cls, values):
        if any(not value.strip() or len(value) > 300 for value in values) or len(set(values)) != len(values):
            raise ValueError("分组 ID 不能为空、过长或重复")
        return sorted(values)

    @model_validator(mode="after")
    def identities(self):
        query = normalized_query(self.query)
        if not query:
            raise ValueError("query 不能为空白")
        query_id = digest({"query": query, "scope": self.scope})
        candidates = tuple(sorted(self.candidates, key=lambda c: c.candidate_id))
        if candidates[0].candidate_id == candidates[1].candidate_id:
            raise ValueError("同一证据不能与自己比较")
        task_id = digest({"query_id": query_id, "candidates": [c.candidate_id for c in candidates]})
        if self.query_id and self.query_id != query_id or self.task_id and self.task_id != task_id:
            raise ValueError("query_id/task_id 与内容不符")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "query_id", query_id)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "candidates", candidates)
        return self


class Manifest(Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    dataset_id: str = Field(min_length=1, max_length=300)
    seed: str = Field(min_length=1, max_length=300)
    grouping_version: Literal["entity-topic-family-neardup-v1"] = GROUPING_VERSION
    # 先形成连通分组，再用 seed 分配 80/10/10；不是按行随机切分。
    manifest_id: str = ""

    @model_validator(mode="after")
    def identity(self):
        expected = digest(self.model_dump(exclude={"manifest_id"}))
        if self.manifest_id and self.manifest_id != expected:
            raise ValueError("manifest_id 不符")
        object.__setattr__(self, "manifest_id", expected)
        return self


class FrozenTask(Model):
    task: Task
    group_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    split: Split
    manifest_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class Presentation(Model):
    presentation_id: str = Field(min_length=1, max_length=100)
    task_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    annotator_id: str = Field(min_length=1, max_length=200)
    left_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    right_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间必须包含时区")
        return value


class LabelSource(Model):
    kind: Literal["human", "ai_weak"]
    labeler_id: str = Field(min_length=1, max_length=200)
    model_name: str | None = Field(default=None, max_length=200)
    model_version: str | None = Field(default=None, max_length=200)
    prompt_version: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def weak_provenance(self):
        if self.kind == "ai_weak" and not self.model_name:
            raise ValueError("AI 弱标签须注明模型名")
        if self.kind == "human" and any((self.model_name, self.model_version, self.prompt_version)):
            raise ValueError("human 不能携带模型标签来源")
        return self


class LabelEvent(Model):
    event_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100)
    task_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    presentation_id: str = Field(min_length=1, max_length=100)
    source: LabelSource
    decision: Decision
    winner_id: str | None = None
    revision: int = Field(ge=1)
    created_at: datetime
    split: Split
    group_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_id: str = Field(pattern=r"^[a-f0-9]{64}$")

    _timezone = field_validator("created_at")(Presentation.timezone_required.__func__)


class LabelRequest(Model):
    presentation_id: str = Field(min_length=1, max_length=100)
    decision: Decision
    expected_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=100)


class GenerateRequest(Model):
    query: str = Field(min_length=1, max_length=3000)
    scope: dict[str, JsonValue] = Field(default_factory=dict)

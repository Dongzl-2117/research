"""实验文件契约；参数越界、未知字段和非法范围一律拒绝。"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from researchlib.retrieval import Query, ScoreContext, ScoreParameters
from researchlib.sources import Source
from researchlib.web.queries import Scope

VERSION = "lexical-snapshot-v1"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Tuning(Model):
    k1: float = Field(1.2, ge=0.1, le=3)
    b: float = Field(0.4, ge=0, le=1)
    title_tf: float = Field(0.5, ge=0, le=2)
    title_bonus: float = Field(0.3, ge=0, le=2)
    phrase_bonus: float = Field(0.5, ge=0, le=2)
    coverage_power: float = Field(2, ge=0.5, le=4)
    min_score_ratio: float = Field(0.12, ge=0, le=0.5)
    passages: StrictInt = Field(24, ge=1, le=80)
    chars: StrictInt = Field(24000, ge=1000, le=32000)
    per_article: StrictInt = Field(3, ge=1, le=6)
    prompt_note: str = Field("", max_length=2000)

    @field_validator(
        "k1", "b", "title_tf", "title_bonus", "phrase_bonus", "coverage_power", "min_score_ratio", mode="before",
    )
    @classmethod
    def numeric(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("参数必须是数字")
        return value

    def scoring(self) -> ScoreParameters:
        return ScoreParameters(**{name: getattr(self, name) for name in ScoreParameters.__dataclass_fields__})


class ScopeSpec(Model):
    source_ids: list[str] = Field(min_length=1, max_length=30)
    company_id: StrictInt | None = Field(None, gt=0, le=2**63 - 1)
    article_id: StrictInt | None = Field(None, gt=0, le=2**63 - 1)
    tree_id: StrictInt | None = Field(None, gt=0, le=2**63 - 1)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def valid(self):
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("来源不能重复")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("开始日期不能晚于结束日期")
        return self

    def scope(self) -> Scope:
        return Scope(**self.model_dump())


class QuerySpec(Model):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    query: str = Field(min_length=1, max_length=500)
    scope: ScopeSpec

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


class Row(Model):
    article_id: StrictInt = Field(gt=0)
    ord: StrictInt = Field(ge=0)
    heading: str = ""
    body: str = Field(max_length=200000)
    title: str
    source_id: str
    published_at: datetime | None = None
    lang: str = ""
    anchor: str = ""

    @field_validator("published_at")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("原文时间必须带时区")
        return value


class FrozenCase(Model):
    spec: QuerySpec
    query: Query
    qa_query: Query
    rows: list[Row] = Field(max_length=10000)
    qa_rows: list[Row] = Field(max_length=10000)
    context: ScoreContext
    qa_context: ScoreContext
    # 前后文也是快照的一部分，编号与内容版本由整个文件 hash 绑定。
    neighbours: list[Row] = Field(default_factory=list, max_length=20000)

    @model_validator(mode="after")
    def scoped(self):
        for row in self.rows + self.qa_rows + self.neighbours:
            if row.source_id not in self.spec.scope.source_ids:
                raise ValueError("候选越过来源范围")
            if self.spec.scope.article_id and row.article_id != self.spec.scope.article_id:
                raise ValueError("候选越过文章范围")
        for context in (self.context, self.qa_context):
            if context.avgdl <= 0 or context.as_of is None or context.as_of.tzinfo is None:
                raise ValueError("评分上下文必须冻结平均长度和带时区的时间")
        return self


class Snapshot(Model):
    schema_version: Literal[1] = 1
    recipe: Literal["lexical-snapshot-v1"] = VERSION
    material: Literal["synthetic", "private_corpus"]
    created_at: datetime
    code_revision: str
    sources: list[Source] = Field(min_length=1, max_length=30)
    cases: list[FrozenCase] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def identities(self):
        ids = [c.spec.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("问题编号不能重复")
        allowed = {s.id for s in self.sources if s.enabled}
        if len(allowed) != len(self.sources):
            raise ValueError("快照不能包含停用或重复来源")
        for case in self.cases:
            if not set(case.spec.scope.source_ids) <= allowed:
                raise ValueError("问题范围含未授权来源")
        return self

    @property
    def fingerprint(self):
        return digest(self.model_dump(mode="json"))


class CompareRequest(Model):
    baseline: Tuning = Field(default_factory=Tuning)
    candidate: Tuning = Field(default_factory=Tuning)
    case_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("case_ids")
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("问题不能重复")
        return value


class RatingRequest(Model):
    case_id: str
    target: Literal["search", "retrieval", "answer"]
    decision: Literal["baseline", "candidate", "tie", "neither", "skip", "uncertain", "withdraw"]
    note: str = Field("", max_length=2000)
    expected_revision: StrictInt = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=100)


class GenerateRequest(Model):
    case_id: str
    acknowledge_cost: Literal[True]
    request_id: str = Field(min_length=8, max_length=100)


class Answer(Model):
    text: str = Field(min_length=1, max_length=60000)
    model: str = Field(min_length=1, max_length=200)
    model_version: str = Field(min_length=1, max_length=200)
    input_tokens: StrictInt | None = Field(None, ge=0)
    output_tokens: StrictInt | None = Field(None, ge=0)
    cost: float | None = Field(None, ge=0)
    currency: str | None = Field(None, max_length=20)

    @model_validator(mode="after")
    def currency_required(self):
        if self.cost is not None and not self.currency:
            raise ValueError("金额必须带币种；未知费用保留空值")
        return self


def now():
    return datetime.now(UTC).isoformat()

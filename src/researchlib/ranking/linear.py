"""纯 CPU 的确定性 pairwise logistic 基线；不接数据库、文件或生产开关。

固定的 query-candidate 交叉特征用于验证偏好学习闭环，不提供双语语义理解保证。
所有词法交叉特征统一使用完整 query.text；topic_text 仅为召回元数据，不参与预测。
明确偏好只训练相对分数，tie/both_irrelevant/cannot_judge 不转换成绝对相关标签。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from researchlib.ranking.contracts import CHOICES, LABEL_SOURCES, RankCandidate, RankQuery, RerankScore

FEATURE_VERSION = "research-linear-query-window-v1"
MODEL_VERSION = "research-linear-pairwise-v1"
FEATURE_NAMES = (
    "query_center_words", "query_context_words", "query_heading_words", "query_title_words",
    "query_center_char2", "query_context_char2", "query_heading_char2", "query_title_char2",
    "query_center_char3", "query_context_char3",
    "query_center_phrase", "query_context_phrase", "query_heading_phrase", "query_title_phrase",
    "center_length", "context_length", "center_query_length_ratio", "query_present",
    "lexical_score", "lexical_missing", "semantic_score", "semantic_missing",
)
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_PREFERENCES = frozenset({"a_better", "b_better"})
_MATERIALS = frozenset({"synthetic", "private_corpus"})


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是有限数字")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数字")
    return number


def _integer(value, name: str, *, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise ValueError(f"{name} 必须是至少为 {low} 的整数")
    return value


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _normal(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _grams(text: str, n: int) -> set[str]:
    return {word[i:i + n] for word in _WORDS.findall(text) for i in range(len(word) - n + 1)}


def _coverage(query: set[str], candidate: set[str]) -> float:
    return len(query & candidate) / len(query) if query else 0.0


def feature_values(query: RankQuery, candidate: RankCandidate) -> tuple[float, ...]:
    """只从查询、当前窗口文字及可选召回分构造固定特征；ID 仅用于契约校验。"""
    if candidate.query_id != query.query_id:
        raise ValueError("候选不属于当前查询")
    if not isinstance(query.text, str) or not query.text.strip():
        raise ValueError("查询必须包含文本")
    # 直接构造的 dataclass 也须经过片段角色、长度和版本校验。
    RankCandidate.from_record(candidate.to_record())
    for name in ("title", "heading", "text", "before", "after"):
        if not isinstance(getattr(candidate, name), str):
            raise ValueError(f"候选 {name} 必须是字符串")
    full_query = _normal(query.text)
    center = _normal(candidate.text)
    context = _normal(candidate.before + "\n" + candidate.after)
    heading, title = _normal(candidate.heading), _normal(candidate.title)
    texts = (center, context, heading, title)
    words = set(_WORDS.findall(full_query))
    grams2, grams3 = _grams(full_query, 2), _grams(full_query, 3)
    lexical = None if candidate.lexical_score is None else _finite(candidate.lexical_score, "lexical_score")
    semantic = None if candidate.semantic_score is None else _finite(candidate.semantic_score, "semantic_score")
    values = (
        *(_coverage(words, set(_WORDS.findall(text))) for text in texts),
        *(_coverage(grams2, _grams(text, 2)) for text in texts),
        *(_coverage(grams3, _grams(text, 3)) for text in texts[:2]),
        *(float(full_query in text) for text in texts),
        min(math.log1p(len(center)) / math.log1p(16000), 1.0),
        min(math.log1p(len(context)) / math.log1p(16000), 1.0),
        min(len(center) / len(full_query), 32.0) / 32.0,
        float(bool(full_query)),
        math.tanh(lexical) if lexical is not None else 0.0,
        float(lexical is None),
        math.tanh(semantic) if semantic is not None else 0.0,
        float(semantic is None),
    )
    if len(values) != len(FEATURE_NAMES) or not all(math.isfinite(x) for x in values):
        raise ValueError("特征必须符合当前版本且全部有限")
    return values


@dataclass(frozen=True)
class PairwiseExample:
    query: RankQuery
    a: RankCandidate
    b: RankCandidate
    choice: str
    source: str
    material: str


def _counts(values: Mapping, keys: frozenset[str], name: str) -> tuple[tuple[str, int], ...]:
    if not isinstance(values, Mapping) or set(values) != keys:
        raise ValueError(f"{name} 的类别必须与契约一致")
    return tuple((key, _integer(values[key], name)) for key in sorted(keys))


@dataclass(frozen=True)
class TrainingSummary:
    data_hash: str
    input_pairs: int
    used_pairs: int
    ignored_choices: int
    filtered_sources: int
    filtered_materials: int
    choice_counts: tuple[tuple[str, int], ...]
    source_counts: tuple[tuple[str, int], ...]
    used_source_counts: tuple[tuple[str, int], ...]
    material_counts: tuple[tuple[str, int], ...]
    used_material_counts: tuple[tuple[str, int], ...]
    seed: int
    epochs: int
    learning_rate: float
    l2: float
    allow_synthetic: bool
    ai_weight: float
    loss: float

    def __post_init__(self):
        if not isinstance(self.data_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", self.data_hash):
            raise ValueError("data_hash 必须是 SHA256")
        for name in ("input_pairs", "used_pairs", "ignored_choices", "filtered_sources", "filtered_materials", "seed"):
            _integer(getattr(self, name), name)
        _integer(self.epochs, "epochs", low=1)
        if not isinstance(self.allow_synthetic, bool):
            raise ValueError("allow_synthetic 必须是布尔值")
        if _finite(self.learning_rate, "learning_rate") <= 0 or _finite(self.l2, "l2") < 0:
            raise ValueError("learning_rate 必须为正，l2 不能为负")
        if not 0 <= _finite(self.ai_weight, "ai_weight") <= 0.25:
            raise ValueError("AI 弱标权重必须在 0 到 0.25 之间")
        if _finite(self.loss, "loss") < 0:
            raise ValueError("loss 不能为负")
        choices = dict(self.choice_counts)
        sources, used = dict(self.source_counts), dict(self.used_source_counts)
        materials, used_materials = dict(self.material_counts), dict(self.used_material_counts)
        for values, keys, name in ((choices, CHOICES, "choice_counts"),
                                  (sources, LABEL_SOURCES, "source_counts"),
                                  (used, LABEL_SOURCES, "used_source_counts"),
                                  (materials, _MATERIALS, "material_counts"),
                                  (used_materials, _MATERIALS, "used_material_counts")):
            if _counts(values, keys, name) != getattr(self, name):
                raise ValueError(f"{name} 必须是类别完整且排序固定的不可变计数")
        if (sum(choices.values()) != self.input_pairs or sum(sources.values()) != self.input_pairs
                or sum(materials.values()) != self.input_pairs or sum(used_materials.values()) != self.used_pairs
                or sum(used.values()) != self.used_pairs or self.used_pairs < 1
                or (self.used_pairs + self.ignored_choices + self.filtered_sources + self.filtered_materials
                    != self.input_pairs)
                or self.ignored_choices != sum(choices[key] for key in CHOICES - _PREFERENCES)
                or any(used[key] > sources[key] for key in LABEL_SOURCES)
                or any(used_materials[key] > materials[key] for key in _MATERIALS)):
            raise ValueError("训练计数不一致或没有明确偏好")
        if (used["synthetic"] and not self.allow_synthetic) or (used["ai"] and not self.ai_weight):
            raise ValueError("训练来源与显式允许策略不一致")
        if used_materials["synthetic"] and not self.allow_synthetic:
            raise ValueError("合成材料必须显式允许后才能用于训练")

    def to_record(self) -> dict:
        return {**self.__dict__, "choice_counts": dict(self.choice_counts),
                "source_counts": dict(self.source_counts), "used_source_counts": dict(self.used_source_counts),
                "material_counts": dict(self.material_counts), "used_material_counts": dict(self.used_material_counts)}

    @classmethod
    def from_record(cls, record: dict) -> TrainingSummary:
        if not isinstance(record, dict) or set(record) != set(cls.__dataclass_fields__):
            raise ValueError("训练摘要字段不符合契约")
        fields = dict(record)
        for name, keys in (("choice_counts", CHOICES), ("source_counts", LABEL_SOURCES),
                           ("used_source_counts", LABEL_SOURCES), ("material_counts", _MATERIALS),
                           ("used_material_counts", _MATERIALS)):
            fields[name] = _counts(fields[name], keys, name)
        return cls(**fields)


def _dot(weights, features) -> float:
    try:
        value = math.fsum(w * x for w, x in zip(weights, features, strict=True))
    except (OverflowError, ValueError) as exc:
        raise ValueError("线性分数溢出或特征维度错误") from exc
    return _finite(value, "线性分数")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 重复字段：{key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class LinearRanker:
    weights: tuple[float, ...]
    summary: TrainingSummary

    def __post_init__(self):
        if not isinstance(self.weights, tuple) or len(self.weights) != len(FEATURE_NAMES):
            raise ValueError("权重必须是当前版本的固定维度元组")
        for value in self.weights:
            _finite(value, "weight")
        if not isinstance(self.summary, TrainingSummary):
            raise ValueError("缺少有效的训练摘要")

    @property
    def model_id(self) -> str:
        return MODEL_VERSION

    @property
    def model_revision(self) -> str:
        return _hash(self.to_record())

    def score(self, query: RankQuery, candidates: tuple[RankCandidate, ...]) -> list[RerankScore]:
        result, seen = [], set()
        for candidate in candidates:
            values = feature_values(query, candidate)
            if candidate.candidate_id in seen:
                raise ValueError("重排候选 ID 重复")
            seen.add(candidate.candidate_id)
            result.append(RerankScore(candidate.candidate_id, _dot(self.weights, values), candidate.window_hash))
        return result

    def to_record(self) -> dict:
        return {"model_version": MODEL_VERSION, "feature_version": FEATURE_VERSION,
                "weights": dict(zip(FEATURE_NAMES, self.weights, strict=True)),
                "training_summary": self.summary.to_record()}

    def to_json(self) -> str:
        return _json(self.to_record())

    @classmethod
    def from_record(cls, record: dict) -> LinearRanker:
        if not isinstance(record, dict) or set(record) != {
            "model_version", "feature_version", "weights", "training_summary"
        }:
            raise ValueError("模型字段不符合契约")
        if record["model_version"] != MODEL_VERSION or record["feature_version"] != FEATURE_VERSION:
            raise ValueError("不支持的模型或特征版本")
        weights = record["weights"]
        if not isinstance(weights, dict) or set(weights) != set(FEATURE_NAMES):
            raise ValueError("权重特征必须与当前版本完全一致")
        return cls(tuple(_finite(weights[name], "weight") for name in FEATURE_NAMES),
                   TrainingSummary.from_record(record["training_summary"]))

    @classmethod
    def from_json(cls, payload: str) -> LinearRanker:
        def reject_constant(value):
            raise ValueError(f"JSON 不允许非有限数字：{value}")

        return cls.from_record(json.loads(payload, object_pairs_hook=_unique_object, parse_constant=reject_constant))


def fit(
    examples: Iterable[PairwiseExample], *, seed: int = 0, epochs: int = 200,
    learning_rate: float = 0.1, l2: float = 0.01,
    allow_synthetic: bool = False, ai_weight: float = 0.0,
) -> LinearRanker:
    """仅在显式 A/B 偏好上训练；本函数不认证标签来源，调用方负责可信操作链。

    seed 只影响局部随机数生成器的训练顺序。不接收数据 split；调用方必须仅传已冻结的训练集。
    摘要哈希覆盖输入快照但不保存快照；模型本身不包含语料或任何 ID 预测特征。
    来源与材料分开门控：人工标注合成材料仍需 allow_synthetic；AI 还需显式非零低权重。
    非偏好先计 ignored_choices，其次检查材料，再检查来源，三个过滤计数不重复相加。
    """
    _integer(seed, "seed")
    _integer(epochs, "epochs", low=1)
    learning_rate, l2 = _finite(learning_rate, "learning_rate"), _finite(l2, "l2")
    ai_weight = _finite(ai_weight, "ai_weight")
    if learning_rate <= 0 or l2 < 0 or not 0 <= ai_weight <= 0.25 or not isinstance(allow_synthetic, bool):
        raise ValueError("训练参数或来源策略无效；AI 弱标权重上限为 0.25")
    choices, sources, used_sources = Counter(), Counter(), Counter()
    materials, used_materials = Counter(), Counter()
    pairs, snapshots = [], []
    ignored = filtered = filtered_materials = 0
    for example in examples:
        if (not isinstance(example, PairwiseExample) or example.choice not in CHOICES
                or example.source not in LABEL_SOURCES or example.material not in _MATERIALS):
            raise ValueError("偏好记录的 choice、source 或 material 不符合契约")
        a, b = feature_values(example.query, example.a), feature_values(example.query, example.b)
        if example.a.candidate_id == example.b.candidate_id:
            raise ValueError("偏好两端不能是同一个候选")
        choices[example.choice] += 1
        sources[example.source] += 1
        materials[example.material] += 1
        if example.choice in _PREFERENCES:
            winner, loser = (example.a, example.b) if example.choice == "a_better" else (example.b, example.a)
            snapshot = {"winner": winner.to_record(), "loser": loser.to_record(), "choice": "preferred"}
        else:
            ends = sorted((example.a.to_record(), example.b.to_record()), key=_json)
            snapshot = {"candidates": ends, "choice": example.choice}
        snapshots.append(_hash({**snapshot, "query": example.query.to_record(),
                                "source": example.source, "material": example.material}))
        if example.choice not in _PREFERENCES:
            ignored += 1
            continue
        if example.material == "synthetic" and not allow_synthetic:
            filtered_materials += 1
            continue
        weight = 1.0 if example.source == "user" or (example.source == "synthetic" and allow_synthetic) else ai_weight
        if example.source == "synthetic" and not allow_synthetic:
            weight = 0.0
        if weight == 0:
            filtered += 1
            continue
        preferred, other = (a, b) if example.choice == "a_better" else (b, a)
        pairs.append((tuple(x - y for x, y in zip(preferred, other, strict=True)), weight))
        used_sources[example.source] += 1
        used_materials[example.material] += 1
    if not pairs:
        raise ValueError("没有可训练的明确偏好；检查标签类别和来源策略")
    pairs.sort()
    rng = random.Random(seed)
    weights = [0.0] * len(FEATURE_NAMES)
    order = list(range(len(pairs)))
    for _ in range(epochs):
        rng.shuffle(order)
        for index in order:
            features, weight = pairs[index]
            margin = _dot(weights, features)
            exp = math.exp(-abs(margin))
            residual = exp / (1 + exp) if margin >= 0 else 1 / (1 + exp)
            weights = [_finite(w + learning_rate * (weight * residual * x - l2 * w), "训练权重")
                       for w, x in zip(weights, features, strict=True)]
    losses = []
    for features, weight in pairs:
        margin = _dot(weights, features)
        losses.append(weight * (max(-margin, 0) + math.log1p(math.exp(-abs(margin)))))
    loss = _finite(math.fsum(losses) / len(pairs)
                   + 0.5 * l2 * math.fsum(w * w for w in weights), "loss")
    summary = TrainingSummary(
        data_hash=_hash(sorted(snapshots)), input_pairs=sum(sources.values()), used_pairs=len(pairs),
        ignored_choices=ignored, filtered_sources=filtered, filtered_materials=filtered_materials,
        choice_counts=tuple((key, choices[key]) for key in sorted(CHOICES)),
        source_counts=tuple((key, sources[key]) for key in sorted(LABEL_SOURCES)),
        used_source_counts=tuple((key, used_sources[key]) for key in sorted(LABEL_SOURCES)),
        material_counts=tuple((key, materials[key]) for key in sorted(_MATERIALS)),
        used_material_counts=tuple((key, used_materials[key]) for key in sorted(_MATERIALS)),
        seed=seed, epochs=epochs, learning_rate=learning_rate, l2=l2,
        allow_synthetic=allow_synthetic, ai_weight=ai_weight, loss=loss,
    )
    return LinearRanker(tuple(weights), summary)

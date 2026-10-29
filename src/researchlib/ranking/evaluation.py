"""纯 CPU 留出对比评估；不训练、不划分数据、不读写文件，也不主动调用模型服务。

调用方只应传入经权威分组审计的 validation 或 test 集，并提供本地 score 实现。
PairwiseExample 不携带 split，因此本函数不能独自证明训练集与留出集没有混合。
"""
from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Iterable

from researchlib.ranking.contracts import CHOICES, LABEL_SOURCES, RankQuery, Reranker, RerankScore
from researchlib.ranking.linear import FEATURE_NAMES, FEATURE_VERSION, PairwiseExample, feature_values

_MATERIALS = ("private_corpus", "synthetic")
_SOURCES = tuple(sorted(LABEL_SOURCES))
_DIRECTIONAL = frozenset({"a_better", "b_better"})
_CENTER_CHAR2 = FEATURE_NAMES.index("query_center_char2")


class _LexicalOverlap:
    """固定的完整查询与中心原文二字组覆盖率；没有训练，也不宣称语义理解。"""
    model_id = "lexical-center-char2-overlap"
    model_revision = FEATURE_VERSION

    def score(self, query, candidates):
        return [RerankScore(c.candidate_id, feature_values(query, c)[_CENTER_CHAR2], c.window_hash)
                for c in candidates]


def _identity(model) -> dict:
    fields = {name: getattr(model, name, None) for name in ("model_id", "model_revision")}
    if not callable(getattr(model, "score", None)) or any(
        not isinstance(value, str) or not value.strip() for value in fields.values()
    ):
        raise ValueError("评估器需要带 model_id、model_revision 和 score 的本地重排器")
    return fields


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _latency(values: list[float]) -> dict:
    return {"calls": len(values), "p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95),
            "min": min(values) if values else None, "max": max(values) if values else None}


def _score(model, example: PairwiseExample) -> dict:
    # 固定输入顺序；输出必须按候选 ID 对回原始 A/B，不能把模型输出第一项当 A。
    candidates = tuple(sorted((example.a, example.b), key=lambda candidate: candidate.candidate_id))
    expected = {candidate.candidate_id: candidate.window_hash for candidate in candidates}
    started = time.perf_counter_ns()
    output = model.score(example.query, candidates)
    elapsed = (time.perf_counter_ns() - started) / 1000000
    if elapsed < 0:
        raise ValueError("测量时钟倒退，不能生成有效延迟报告")
    if not isinstance(output, list | tuple) or len(output) != 2:
        raise ValueError("评估评分必须完整返回两个候选，不能使用部分结果")
    scores = {}
    for item in output:
        if (not isinstance(item, RerankScore) or item.candidate_id not in expected or item.candidate_id in scores
                or item.window_hash != expected[item.candidate_id] or isinstance(item.score, bool)
                or not isinstance(item.score, int | float) or not math.isfinite(item.score)):
            raise ValueError("评估评分包含无效 ID、重复 ID、窗口版本或非有限分数")
        scores[item.candidate_id] = float(item.score)
    if set(scores) != set(expected):
        raise ValueError("评估评分缺少候选 ID")
    a_score, b_score = scores[example.a.candidate_id], scores[example.b.candidate_id]
    winner = None if a_score == b_score else (example.a.candidate_id if a_score > b_score else example.b.candidate_id)
    return {"winner_id": winner, "score_tie": winner is None, "latency_ms": elapsed}


def _metrics(rows: list[dict], key: str) -> dict:
    counts = Counter(row["choice"] for row in rows)
    directional = [row for row in rows if row["choice"] in _DIRECTIONAL]
    tied = sum(row[key]["score_tie"] for row in directional)
    correct = sum(row[key]["winner_id"] == row["label_winner_id"] for row in directional)
    return {
        "pairs": len(rows), "choice_counts": {choice: counts[choice] for choice in sorted(CHOICES)},
        "directional_pairs": len(directional), "correct": correct,
        "incorrect": len(directional) - correct - tied, "directional_score_ties": tied,
        # 并列仍占方向题分母，不能把没有区分优胜者的分数计为方向正确。
        "directional_accuracy": correct / len(directional) if directional else None,
        "non_directional_counts": {choice: counts[choice] for choice in ("tie", "both_irrelevant", "cannot_judge")},
        "score_ties": sum(row[key]["score_tie"] for row in rows),
        "latency_ms": _latency([row[key]["latency_ms"] for row in rows]),
    }


def _report(rows: list[dict], key: str, identity: dict) -> dict:
    return {
        **identity,
        "by_source": {source: _metrics([row for row in rows if row["source"] == source], key) for source in _SOURCES},
        "by_material": {kind: _metrics([row for row in rows if row["material"] == kind], key) for kind in _MATERIALS},
        "by_source_material": {
            source: {kind: _metrics([row for row in rows if row["source"] == source and row["material"] == kind], key)
                     for kind in _MATERIALS} for source in _SOURCES
        },
        "latency_ms": _latency([row[key]["latency_ms"] for row in rows]),
    }


def evaluate_pairs(model: Reranker, examples: Iterable[PairwiseExample], *, baseline: Reranker | None = None) -> dict:
    """评估调用方给定的单个留出集；baseline=None 使用固定二字组词法覆盖率。

    user、ai 弱标、synthetic 来源及 synthetic/private_corpus 材料均分别统计。
    方向准确率分母只有明确 A/B 偏好，并列分数占分母但不算正确。其他选择只保留计数。
    延迟只测真实 score 调用，单位毫秒，p50/p95 为观测样本排序后的线性插值分位数。
    不执行任何训练；冻结划分、来源认证和实体/主题冷启动由外部审计负责。
    """
    comparator = _LexicalOverlap() if baseline is None else baseline
    identities = {"model": _identity(model), "baseline": _identity(comparator)}
    pairs = list(examples)
    # 先检查全部输入，避免无效后缀导致半批结果被误当作完整评测。
    for example in pairs:
        if (not isinstance(example, PairwiseExample) or example.choice not in CHOICES
                or example.source not in LABEL_SOURCES or example.material not in _MATERIALS):
            raise ValueError("评估偏好的 choice、source 或 material 不符合契约")
        RankQuery.from_record(example.query.to_record())
        feature_values(example.query, example.a)
        feature_values(example.query, example.b)
        if example.a.candidate_id == example.b.candidate_id:
            raise ValueError("评估两端不能是同一个候选")
    rows = []
    for example in pairs:
        label_winner = (example.a.candidate_id if example.choice == "a_better" else example.b.candidate_id
                        if example.choice == "b_better" else None)
        rows.append({
            "query_id": example.query.query_id, "a_id": example.a.candidate_id, "b_id": example.b.candidate_id,
            "choice": example.choice, "source": example.source, "material": example.material,
            "label_winner_id": label_winner, "model": _score(model, example),
            "baseline": _score(comparator, example),
        })
    for key, ranker in (("model", model), ("baseline", comparator)):
        if _identity(ranker) != identities[key]:
            raise ValueError("评估期间模型版本改变，不能把更新后的模型结果混入留出集评测")
    sources = Counter(example.source for example in pairs)
    materials = Counter(example.material for example in pairs)
    return {
        "pair_count": len(pairs), "partition_verification": "caller_required",
        "source_counts": {source: sources[source] for source in _SOURCES},
        "material_counts": {kind: materials[kind] for kind in _MATERIALS},
        "model": _report(rows, "model", identities["model"]),
        "baseline": _report(rows, "baseline", identities["baseline"]),
        "predictions": rows,
        "no_answer": {
            "both_irrelevant_pairs_by_source": {
                source: sum(row["source"] == source and row["choice"] == "both_irrelevant" for row in rows)
                for source in _SOURCES
            },
            "calibrated_absolute_abstention": False, "abstention_accuracy": None,
            "reason": "neither/both_irrelevant 只表示两段都不适合；pairwise 排序器没有校准过的绝对拒答能力。",
        },
        "latency_measurement": "每对调用 score(query, 按 candidate_id 排序的两候选)，只计 score 本身；分位数线性插值。",
        "limitations": [
            "本函数不训练、不读取或合并训练数据。PairwiseExample 没有 split，调用方必须先按冻结清单隔离留出集。",
            "user 与 ai 弱标、synthetic 标签来源分开；本函数不认证人工操作链，AI 弱标一致率不是人工正确率。",
            "synthetic/private_corpus 材料分开；合成成绩只验证流程，不能证明真实泛化、双语理解或引用真实性。",
            "tie/both_irrelevant/cannot_judge 不转换成 A/B 优胜或绝对相关性，也不计作方向正确。",
            "延迟来自本进程这批 score 调用，少量 CPU 样本不能代表线上服务延迟。",
        ],
    }

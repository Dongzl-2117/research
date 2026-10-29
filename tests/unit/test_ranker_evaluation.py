"""合成留出样本只验证评估算术、来源隔离和评分契约，不代表真实泛化。"""
import json
import math
from dataclasses import replace

import pytest

from researchlib.ranking import evaluation
from researchlib.ranking.contracts import RankCandidate, RankQuery, RerankScore, SourceFragment
from researchlib.ranking.evaluation import evaluate_pairs
from researchlib.ranking.linear import PairwiseExample, fit


def _example(name="one", *, source="user", material="synthetic", choice="a_better"):
    query = RankQuery(f"query-{name}", "产品 客户留存", "客户留存", (f"entity-{name}",), "{}")

    def candidate(side, text):
        fragment = SourceFragment("center", 0, 0, len(text), text, f"chunk-version-{side}")
        return RankCandidate(
            query.query_id, "synthetic-source", f"article-{name}-{side}", f"family-{name}-{side}",
            "source-version", "合成经营资料", "", (fragment,), query.entity_ids, "正文", "/synthetic",
            opaque_id=f"{side}-{name}",
        )

    return PairwiseExample(query, candidate("a", "产品通过迁移成本保持客户留存。"),
                           candidate("b", "办公区本周安排班车与午餐。"), choice, source, material)


class _FixedScores:
    model_id = "cpu-stub"
    model_revision = "revision-one"

    def __init__(self, a=2.0, b=1.0):
        self.values = {"a": a, "b": b}
        self.calls = []

    def score(self, query, candidates):
        self.calls.append((query, candidates))
        result = [RerankScore(c.candidate_id, self.values[c.candidate_id[0]], c.window_hash) for c in candidates]
        return sorted(result, key=lambda item: item.score, reverse=True)


def test_directional_accuracy_matches_ids_even_when_output_and_ab_are_reversed():
    first = _example()
    swapped = replace(first, a=first.b, b=first.a, choice="b_better")
    model = _FixedScores(a=1, b=3)
    result = evaluate_pairs(model, [first, swapped])
    metrics = result["model"]["by_source"]["user"]
    assert metrics["directional_pairs"] == 2 and metrics["incorrect"] == 2 and metrics["correct"] == 0
    assert metrics["directional_accuracy"] == 0
    assert all(row["model"]["winner_id"] == first.b.candidate_id for row in result["predictions"])
    assert all([c.candidate_id for c in candidates] == sorted(c.candidate_id for c in candidates)
               for _query, candidates in model.calls)


def test_equal_scores_are_ties_and_stay_in_directional_denominator():
    result = evaluate_pairs(_FixedScores(a=-2, b=-2), [_example(), _example("two", choice="b_better")])
    metrics = result["model"]["by_source"]["user"]
    assert metrics["correct"] == metrics["incorrect"] == 0
    assert metrics["directional_score_ties"] == metrics["score_ties"] == 2
    assert metrics["directional_accuracy"] == 0
    assert all(row["model"]["winner_id"] is None for row in result["predictions"])


def test_non_directional_labels_are_counts_without_invented_no_answer_accuracy():
    examples = [_example("tie", choice="tie"), _example("neither", choice="both_irrelevant"),
                _example("uncertain", choice="cannot_judge")]
    result = evaluate_pairs(_FixedScores(), examples)
    metrics = result["model"]["by_source"]["user"]
    assert metrics["directional_pairs"] == metrics["correct"] == 0
    assert metrics["directional_accuracy"] is None
    assert metrics["non_directional_counts"] == {"tie": 1, "both_irrelevant": 1, "cannot_judge": 1}
    assert result["no_answer"]["both_irrelevant_pairs_by_source"]["user"] == 1
    assert result["no_answer"]["abstention_accuracy"] is None
    assert not result["no_answer"]["calibrated_absolute_abstention"]
    assert "绝对拒答能力" in result["no_answer"]["reason"]
    assert all(row["label_winner_id"] is None for row in result["predictions"])


def test_sources_and_materials_have_separate_and_crossed_metrics():
    # private_corpus 仅模拟元数据类型；所有测试文字仍是合成材料。
    examples = [_example("human-private", material="private_corpus"),
                _example("human-demo", choice="b_better"),
                _example("weak", source="ai", material="private_corpus", choice="b_better"),
                _example("synthetic-label", source="synthetic")]
    result = evaluate_pairs(_FixedScores(), examples)
    report = result["model"]
    assert result["source_counts"] == {"ai": 1, "synthetic": 1, "user": 2}
    assert result["material_counts"] == {"private_corpus": 2, "synthetic": 2}
    assert report["by_source"]["user"]["directional_accuracy"] == 0.5
    assert report["by_source"]["ai"]["directional_accuracy"] == 0
    assert report["by_source"]["synthetic"]["directional_accuracy"] == 1
    crossed = report["by_source_material"]
    assert crossed["user"]["private_corpus"]["directional_accuracy"] == 1
    assert crossed["user"]["synthetic"]["directional_accuracy"] == 0
    assert crossed["ai"]["synthetic"]["directional_accuracy"] is None


def test_default_baseline_is_fixed_query_center_lexical_overlap():
    example = _example()
    result = evaluate_pairs(_FixedScores(a=0, b=2), [example])
    assert result["model"]["by_source"]["user"]["directional_accuracy"] == 0
    assert result["baseline"]["model_id"] == "lexical-center-char2-overlap"
    assert result["baseline"]["by_source"]["user"]["directional_accuracy"] == 1
    assert result["predictions"][0]["baseline"]["winner_id"] == example.a.candidate_id


def test_custom_baseline_and_empty_input_do_not_trigger_training():
    model, baseline = _FixedScores(), _FixedScores(a=1, b=2)
    result = evaluate_pairs(model, [], baseline=baseline)
    assert not model.calls and not baseline.calls
    assert result["pair_count"] == 0 and result["partition_verification"] == "caller_required"
    assert result["model"]["latency_ms"] == {"calls": 0, "p50": None, "p95": None, "min": None, "max": None}
    assert result["model"]["by_source"]["user"]["directional_accuracy"] is None
    result = evaluate_pairs(model, [_example()], baseline=baseline)
    assert result["model"]["by_source"]["user"]["directional_accuracy"] == 1
    assert result["baseline"]["by_source"]["user"]["directional_accuracy"] == 0


def test_latency_percentiles_use_measured_score_intervals_only(monkeypatch):
    # 注入时钟只核对分位数算术；不是对外报告的真实性能数据。
    ticks = iter([0, 1000000, 1000000, 3000000,
                  3000000, 6000000, 6000000, 10000000,
                  10000000, 15000000, 15000000, 21000000])
    monkeypatch.setattr(evaluation.time, "perf_counter_ns", lambda: next(ticks))
    result = evaluate_pairs(_FixedScores(), [_example(str(i)) for i in range(3)], baseline=_FixedScores())
    assert result["model"]["latency_ms"] == {"calls": 3, "p50": 3, "p95": 4.8, "min": 1, "max": 5}
    assert result["baseline"]["latency_ms"] == {"calls": 3, "p50": 4, "p95": 5.8, "min": 2, "max": 6}


def test_real_cpu_linear_score_runs_without_refit_or_file_io(monkeypatch):
    training = _example("training", material="private_corpus")
    model = fit([training], epochs=2)
    heldout = _example("heldout")
    before = model.to_record()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("留出评估不得训练")

    monkeypatch.setattr("researchlib.ranking.linear.fit", forbidden)
    result = evaluate_pairs(model, [heldout])
    assert model.to_record() == before
    assert result["model"]["latency_ms"]["calls"] == 1
    assert result["model"]["latency_ms"]["p50"] >= 0
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize("broken", ["duplicate", "unknown", "missing", "hash", "nan", "inf", "bool", "string"])
def test_invalid_model_results_are_rejected_as_a_whole(broken):
    class Bad(_FixedScores):
        def score(self, query, candidates):
            good = super().score(query, candidates)
            if broken == "duplicate":
                return [good[0], good[0]]
            if broken == "unknown":
                return [replace(good[0], candidate_id="unknown"), good[1]]
            if broken == "missing":
                return good[:1]
            if broken == "hash":
                return [replace(good[0], window_hash="obsolete"), good[1]]
            value = {"nan": math.nan, "inf": math.inf, "bool": True, "string": "1"}[broken]
            return [replace(good[0], score=value), good[1]]

    with pytest.raises(ValueError, match="评估评分"):
        evaluate_pairs(Bad(), [_example()])


@pytest.mark.parametrize("change", ["choice", "source", "material", "query", "candidate"])
def test_bad_input_is_rejected_before_any_model_call(change):
    good = _example()
    bad = {"choice": replace(good, choice="left"), "source": replace(good, source="unverified"),
           "material": replace(good, material="unknown"),
           "query": replace(good, b=replace(good.b, query_id="wrong-query")),
           "candidate": replace(good, b=good.a)}[change]
    model = _FixedScores()
    with pytest.raises(ValueError):
        evaluate_pairs(model, [good, bad])
    assert model.calls == []


def test_model_version_change_during_evaluation_is_rejected():
    class Mutating(_FixedScores):
        def score(self, query, candidates):
            result = super().score(query, candidates)
            self.model_revision = "changed-by-score"
            return result

    with pytest.raises(ValueError, match="评估期间模型版本改变"):
        evaluate_pairs(Mutating(), [_example()])

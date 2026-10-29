"""只验证小型合成样本上的训练与序列化契约，不代表未见主题或线上质量。"""
import json
import math
import random
from dataclasses import replace

import pytest

from researchlib.ranking.contracts import RankCandidate, RankQuery, SourceFragment
from researchlib.ranking.linear import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    LinearRanker,
    PairwiseExample,
    feature_values,
    fit,
)


def _query(text="轨道软件 用户留存", topic="用户留存", key="q"):
    return RankQuery(key, text, topic, ("entity-a",), "{}", topic_group_id="synthetic-family")


def _candidate(query, text, *, article="doc-a", before="", heading="", **kwargs):
    fragments = [SourceFragment("center", 1, 0, len(text), text, "version-center")]
    if before:
        fragments.insert(0, SourceFragment("before", 0, 0, len(before), before, "version-before"))
    return RankCandidate(
        query.query_id, "synthetic-source", article, article, "source-version", "合成经营资料", heading,
        tuple(fragments), ("entity-a",), "正文", "/synthetic/" + article, **kwargs,
    )


def _example(*, source="user", choice="a_better"):
    query = _query()
    return PairwiseExample(query, _candidate(query, "用户留存提高，因为历史记录迁移需要额外成本。"),
                           _candidate(query, "本周办公区安排了班车与午餐。", article="doc-b"),
                           choice, source, "private_corpus")


def test_pairwise_learning_changes_relative_order_and_preserves_snapshot():
    example = _example()
    model = fit([example], seed=7)
    result = model.score(example.query, (example.b, example.a))
    assert result[1].score > result[0].score
    assert result[1].candidate_id == example.a.candidate_id
    assert result[1].window_hash == example.a.window_hash
    assert model.summary.used_pairs == 1 and model.summary.loss < math.log(2)
    assert model.score(example.query, ()) == []


def test_query_and_candidate_are_featured_together():
    first = _example()
    second_query = _query("轨道软件 班车午餐", "班车午餐", "other-q")
    a = replace(first.a, query_id=second_query.query_id)
    b = replace(first.b, query_id=second_query.query_id)
    second = PairwiseExample(second_query, a, b, "b_better", "user", "private_corpus")
    model = fit([first, second], epochs=300)
    first_scores = model.score(first.query, (first.a, first.b))
    second_scores = model.score(second.query, (a, b))
    assert first_scores[0].score > first_scores[1].score
    assert second_scores[1].score > second_scores[0].score


def test_canonical_preference_is_invariant_to_ab_swap_and_input_order():
    first = _example()
    second = replace(first, query=_query(key="query-two"),
                     a=replace(first.a, query_id="query-two"), b=replace(first.b, query_id="query-two"))
    swapped = replace(first, a=first.b, b=first.a, choice="b_better")
    normal = fit([first, second], seed=3)
    changed = fit([second, swapped], seed=3)
    assert normal.weights == changed.weights
    assert normal.score(first.query, (first.a, first.b)) == changed.score(first.query, (first.a, first.b))
    assert normal.summary.data_hash == changed.summary.data_hash


def test_fit_is_deterministic_and_does_not_touch_global_random_state():
    previous_state = random.getstate()
    try:
        random.seed(32)
        state = random.getstate()
        example = _example()
        first = fit([example], seed=9)
        assert first.to_json() == fit([example], seed=9).to_json()
        assert random.getstate() == state
    finally:
        random.setstate(previous_state)


def test_unordered_labels_are_counted_and_do_not_train_absolute_relevance():
    example = _example()
    others = [replace(example, choice=choice) for choice in ("tie", "both_irrelevant", "cannot_judge")]
    baseline = fit([example])
    mixed = fit([example, *others])
    assert mixed.weights == baseline.weights
    assert mixed.summary.used_pairs == 1 and mixed.summary.ignored_choices == 3
    assert all(dict(mixed.summary.choice_counts)[choice] == 1 for choice in ("tie", "both_irrelevant", "cannot_judge"))
    with pytest.raises(ValueError, match="没有可训练"):
        fit(others)


def test_source_filtering_is_explicit_and_ai_stays_low_weight():
    user = _example()
    synthetic = replace(user, source="synthetic", choice="b_better")
    ai = replace(user, source="ai", choice="b_better")
    default = fit([user, synthetic, ai])
    assert default.weights == fit([user]).weights
    assert default.summary.filtered_sources == 2
    assert dict(default.summary.source_counts) == {"user": 1, "ai": 1, "synthetic": 1}
    assert dict(default.summary.used_source_counts) == {"user": 1, "ai": 0, "synthetic": 0}
    assert fit([synthetic], allow_synthetic=True).summary.used_pairs == 1
    assert fit([ai], ai_weight=0.1).summary.used_pairs == 1
    for examples in ([synthetic], [ai]):
        with pytest.raises(ValueError, match="没有可训练"):
            fit(examples)
    with pytest.raises(ValueError, match="来源策略"):
        fit([ai], ai_weight=1.0)


def test_material_is_gated_independently_of_human_source():
    user = _example()
    synthetic_material = replace(user, material="synthetic", choice="b_better")
    mixed = fit([user, synthetic_material])
    assert mixed.weights == fit([user]).weights
    assert mixed.summary.filtered_materials == 1
    assert dict(mixed.summary.source_counts)["user"] == 2
    assert dict(mixed.summary.used_material_counts) == {"private_corpus": 1, "synthetic": 0}
    with pytest.raises(ValueError, match="没有可训练"):
        fit([synthetic_material])
    assert fit([synthetic_material], allow_synthetic=True).summary.used_pairs == 1


def test_weak_ai_on_synthetic_material_needs_both_permissions_and_keeps_provenance():
    weak = replace(_example(), source="ai", material="synthetic")
    for kwargs in ({}, {"allow_synthetic": True}, {"ai_weight": 0.1}):
        with pytest.raises(ValueError, match="没有可训练"):
            fit([weak], **kwargs)
    model = fit([weak], allow_synthetic=True, ai_weight=0.1)
    assert dict(model.summary.used_source_counts) == {"ai": 1, "synthetic": 0, "user": 0}
    assert dict(model.summary.used_material_counts) == {"private_corpus": 0, "synthetic": 1}
    assert LinearRanker.from_json(model.to_json()).summary == model.summary


def test_features_do_not_use_identifiers_or_source_metadata():
    example = _example()
    query = replace(example.query, query_id="new-query", entity_ids=("new-entity",),
                    topic_group_id="unseen-family", scope_json='{"source_ids":["different"]}')
    candidate = replace(example.a, query_id=query.query_id, source_id="new-source", article_id="new-article",
                        document_family_id="new-family", source_version="new-source-version",
                        entity_ids=("new-entity",), entity_basis="标题", url="/new", published_at="2030-01-01")
    assert feature_values(query, candidate) == feature_values(example.query, example.a)
    assert not any("id" in name or "entity" in name or "source" in name for name in FEATURE_NAMES)


def test_all_lexical_features_use_full_query_and_ignore_topic_metadata():
    example = _example()
    values = feature_values(example.query, example.a)
    model = fit([example])
    assert len(values) == len(FEATURE_NAMES) == len(set(FEATURE_NAMES)) == 22
    assert FEATURE_VERSION == "research-linear-query-window-v1"
    assert not any(name.startswith("topic_") for name in FEATURE_NAMES)
    for topic in ("", "完全不同的召回主题", example.query.text):
        changed = replace(example.query, topic_text=topic)
        assert feature_values(changed, example.a) == values
        assert fit([replace(example, query=changed)]).weights == model.weights
        assert model.score(changed, (example.a, example.b)) == model.score(example.query, (example.a, example.b))


def test_features_distinguish_context_heading_and_missing_semantics():
    example = _example()
    context = replace(example.b, heading="用户留存", fragments=example.b.fragments + (
        SourceFragment("after", 2, 0, 4, "用户留存", "context-version"),
    ))
    values = dict(zip(FEATURE_NAMES, feature_values(example.query, context), strict=True))
    assert values["query_center_words"] == 0
    assert values["query_context_words"] == values["query_heading_words"] == 0.5
    assert values["semantic_missing"] == 1
    present = dict(zip(FEATURE_NAMES, feature_values(example.query, replace(context, semantic_score=0.0)), strict=True))
    assert present["semantic_missing"] == 0 and present["semantic_score"] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, "0.5"])
@pytest.mark.parametrize("field", ["semantic_score", "lexical_score"])
def test_nonfinite_or_non_numeric_candidate_scores_are_rejected(field, bad):
    example = _example()
    with pytest.raises(ValueError, match="有限数字"):
        fit([replace(example, a=replace(example.a, **{field: bad}))])


@pytest.mark.parametrize("field", ["learning_rate", "l2", "ai_weight"])
def test_nonfinite_training_parameters_are_rejected(field):
    with pytest.raises(ValueError, match="有限数字"):
        fit([_example()], **{field: float("nan")})


def test_input_contract_rejects_wrong_query_duplicate_candidate_and_unknown_labels():
    example = _example()
    with pytest.raises(ValueError, match="当前查询"):
        fit([replace(example, b=replace(example.b, query_id="other"))])
    with pytest.raises(ValueError, match="同一个候选"):
        fit([replace(example, b=example.a)])
    for changed in (replace(example, choice="left"), replace(example, source="model"),
                    replace(example, material="unknown")):
        with pytest.raises(ValueError, match="不符合契约"):
            fit([changed])
    model = fit([example])
    with pytest.raises(ValueError, match="ID 重复"):
        model.score(example.query, (example.a, example.a))


def test_json_roundtrip_is_versioned_finite_and_contains_no_raw_snapshots():
    example = _example()
    model = fit([example], seed=12)
    payload = model.to_json()
    restored = LinearRanker.from_json(payload)
    assert restored.to_json() == payload
    assert restored.model_revision == model.model_revision
    assert restored.score(example.query, (example.a, example.b)) == model.score(example.query, (example.a, example.b))
    assert example.a.text not in payload and example.query.text not in payload
    assert example.a.candidate_id not in payload and example.a.source_id not in payload
    assert set(json.loads(payload)) == {"model_version", "feature_version", "weights", "training_summary"}


@pytest.mark.parametrize("mutation", ["nan", "unknown_feature", "missing_feature", "version", "extra", "counts"])
def test_model_loader_rejects_invalid_weights_versions_and_counts(mutation):
    record = fit([_example()]).to_record()
    if mutation == "nan":
        record["weights"][FEATURE_NAMES[0]] = float("nan")
    elif mutation == "unknown_feature":
        record["weights"]["entity_id"] = 1.0
    elif mutation == "missing_feature":
        record["weights"].pop(FEATURE_NAMES[0])
    elif mutation == "version":
        record["feature_version"] = "future"
    elif mutation == "extra":
        record["training_snapshots"] = []
    else:
        record["training_summary"]["used_pairs"] = 999
    with pytest.raises(ValueError):
        LinearRanker.from_record(record)


def test_json_loader_rejects_nonfinite_literals_and_duplicate_fields():
    model = fit([_example()])
    with pytest.raises(ValueError, match="非有限"):
        LinearRanker.from_json(model.to_json().replace('"query_center_words":', '"query_center_words":NaN,"x":'))
    with pytest.raises(ValueError, match="重复字段"):
        LinearRanker.from_json('{"model_version":"a","model_version":"b"}')


def test_extreme_finite_model_weights_cannot_emit_infinite_scores():
    example = _example()
    model = replace(fit([example]), weights=(1e308,) * len(FEATURE_NAMES))
    with pytest.raises(ValueError, match="溢出|有限数字"):
        model.score(example.query, (example.a,))

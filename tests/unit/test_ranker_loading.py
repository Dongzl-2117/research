"""加载门控用纯模拟快照验证；private_corpus 只是测试分类，不含真实私有资料或人工金标。"""
import json
from pathlib import Path

import pytest

from researchlib.ranking.contracts import RankCandidate, RankQuery, SourceFragment
from researchlib.ranking.linear import FEATURE_NAMES, LinearRanker, PairwiseExample, fit
from researchlib.ranking.runtime import load_reviewed_ranker


def _simulated_model(material="private_corpus"):
    """固定自编文本只用于模拟导入门控，不能计入真实人工训练或评测成绩。"""
    query = RankQuery("simulated-query", "合成产品 客户续约", "", (), "{}")
    candidates = []
    for key, text in (("a", "合成产品的客户续约受历史工作记录迁移成本影响。"),
                      ("b", "合成办公区将调整班车的发车时间。")):
        fragment = SourceFragment("center", 0, 0, len(text), text, "simulated-chunk-version")
        candidates.append(RankCandidate(
            query.query_id, "simulated-source", key, key, "simulated-source-version", "模拟标题", "",
            (fragment,), (), "模拟主体", "/simulated/" + key,
        ))
    example = PairwiseExample(query, candidates[0], candidates[1], "a_better", "user", material)
    return fit([example], epochs=5, allow_synthetic=material == "synthetic")


def _review(model):
    return {
        "approved": True,
        "model_revision": model.model_revision,
        "reviewer_id": "simulated-reviewer",
        "validation_manifest_id": "simulated-validation-manifest",
        "test_manifest_id": "simulated-test-manifest",
        "training_only": False,
    }


def _files(tmp_path, *, material="private_corpus"):
    model = _simulated_model(material)
    model_path, review_path = tmp_path / "model.json", tmp_path / "review.json"
    model_path.write_text(model.to_json(), encoding="utf-8")
    review_path.write_text(json.dumps(_review(model)), encoding="utf-8")
    return model, model_path, review_path


def _assert_rejected(model_path, review_path):
    model, trace = load_reviewed_ranker(model_path, review_path)
    assert model is None and trace == {"status": "rejected"}


def test_default_off_does_not_touch_files_even_if_review_path_is_given(monkeypatch, tmp_path):
    def reject_io(*args, **kwargs):
        raise AssertionError("默认关闭时不应读取任何文件")

    monkeypatch.setattr(Path, "stat", reject_io)
    assert load_reviewed_ranker() == (None, {"status": "disabled"})
    assert load_reviewed_ranker(review_path=tmp_path / "unused-review.json") == (None, {"status": "disabled"})


def test_model_path_without_review_is_insufficient_and_not_read(monkeypatch, tmp_path):
    def reject_io(*args, **kwargs):
        raise AssertionError("没有审阅记录时不应加载模型")

    monkeypatch.setattr(Path, "stat", reject_io)
    assert load_reviewed_ranker(tmp_path / "not-read.json") == (None, {"status": "review_required"})


@pytest.mark.parametrize("shared_manifest", [False, True])
def test_explicitly_reviewed_simulated_private_corpus_model_is_accepted(tmp_path, shared_manifest):
    original, model_path, review_path = _files(tmp_path)
    if shared_manifest:
        review = _review(original)
        review["test_manifest_id"] = review["validation_manifest_id"]
        review_path.write_text(json.dumps(review), encoding="utf-8")
    loaded, trace = load_reviewed_ranker(model_path, review_path)
    assert isinstance(loaded, LinearRanker)
    assert loaded.to_json() == original.to_json()
    assert trace == {"status": "loaded", "model_id": original.model_id, "model_revision": original.model_revision}


def test_synthetic_only_training_cannot_be_loaded_even_with_matching_review(tmp_path):
    model, model_path, review_path = _files(tmp_path, material="synthetic")
    assert dict(model.summary.used_material_counts) == {"private_corpus": 0, "synthetic": 1}
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("field", ["model_version", "feature_version"])
def test_model_and_feature_version_must_be_supported(tmp_path, field):
    model, model_path, review_path = _files(tmp_path)
    record = model.to_record()
    record[field] = "unsupported-future-version"
    model_path.write_text(json.dumps(record), encoding="utf-8")
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, "0.25"])
def test_nonfinite_and_nonnumeric_weights_are_rejected(tmp_path, bad):
    model, model_path, review_path = _files(tmp_path)
    record = model.to_record()
    record["weights"][FEATURE_NAMES[0]] = bad
    model_path.write_text(json.dumps(record), encoding="utf-8")
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("contents", ["{", "null", "[]", "true", '{"weights":{}}', "[" * 1200 + "0" + "]" * 1200])
def test_malformed_model_does_not_load(tmp_path, contents):
    _, model_path, review_path = _files(tmp_path)
    model_path.write_text(contents, encoding="utf-8")
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("contents", ["{", "null", "[]", "true", "[" * 1200 + "0" + "]" * 1200])
def test_malformed_review_does_not_load(tmp_path, contents):
    _, model_path, review_path = _files(tmp_path)
    review_path.write_text(contents, encoding="utf-8")
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("field", ["reviewer_id", "validation_manifest_id", "test_manifest_id"])
@pytest.mark.parametrize("bad", [None, "", "   ", 1, True, ["simulated"], {"id": "simulated"}])
def test_review_identity_and_manifest_fields_must_be_nonblank_strings(tmp_path, field, bad):
    model, model_path, review_path = _files(tmp_path)
    review = _review(model)
    review[field] = bad
    review_path.write_text(json.dumps(review), encoding="utf-8")
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("changes", [
    {"approved": False}, {"approved": 1}, {"approved": "true"}, {"training_only": True},
    {"training_only": 0}, {"model_revision": "obsolete-revision"},
])
def test_review_approval_and_exact_revision_cannot_be_coerced(tmp_path, changes):
    model, model_path, review_path = _files(tmp_path)
    review_path.write_text(json.dumps({**_review(model), **changes}), encoding="utf-8")
    _assert_rejected(model_path, review_path)


def test_review_does_not_authorize_subsequent_model_weight_changes(tmp_path):
    model, model_path, review_path = _files(tmp_path)
    record = model.to_record()
    record["weights"][FEATURE_NAMES[0]] += 0.125
    changed = LinearRanker.from_record(record)
    assert changed.model_revision != model.model_revision
    model_path.write_text(changed.to_json(), encoding="utf-8")
    _assert_rejected(model_path, review_path)


def test_review_rejects_duplicate_fields_and_nonstandard_json_numbers(tmp_path):
    model, model_path, review_path = _files(tmp_path)
    valid = json.dumps(_review(model))
    review_path.write_text(valid.replace('"approved": true', '"approved": false, "approved": true'), encoding="utf-8")
    _assert_rejected(model_path, review_path)
    review_path.write_text(valid[:-1] + ', "metric": NaN}', encoding="utf-8")
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("target", ["model", "review"])
def test_oversized_files_are_rejected_before_deserialization(tmp_path, target):
    _, model_path, review_path = _files(tmp_path)
    path, size = (model_path, 2_000_001) if target == "model" else (review_path, 64_001)
    path.write_text("x" * size, encoding="utf-8")
    _assert_rejected(model_path, review_path)


@pytest.mark.parametrize("target", ["model", "review"])
def test_missing_model_or_review_files_fail_closed(tmp_path, target):
    _, model_path, review_path = _files(tmp_path)
    (model_path if target == "model" else review_path).unlink()
    _assert_rejected(model_path, review_path)

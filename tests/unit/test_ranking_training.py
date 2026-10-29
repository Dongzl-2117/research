"""离线命令的划分/训练记录门禁；全部为模拟合成协议样本，无人工金标。"""
from dataclasses import replace

import pytest

from researchlib.ranking import __main__ as cli
from researchlib.ranking.contracts import RankCandidate, RankQuery, SourceFragment
from researchlib.ranking.linear import PairwiseExample
from researchlib.ranking.relevance_adapter import LabelledPair, PreferenceData


def _data():
    pairs = []
    for split in ("train", "validation", "test"):
        query = RankQuery(f"query-{split}", f"虚构{split}查询", "主题", (split,), "{}")
        candidates = tuple(RankCandidate(
            query.query_id, "synthetic", f"{split}-{i}", f"family-{split}-{i}", "v1", "虚构标题", "",
            (SourceFragment("center", 0, 0, len(text), text, "chunk-v1"),), (split,), "fixture-only",
            "https://example.invalid/source",
        ) for i, text in enumerate((query.text + "正文", "无关记录")))
        pairs.append(LabelledPair(f"task-{split}", split, f"group-{split}", f"event-{split}", "simulated",
                                  "left", 1, PairwiseExample(query, *candidates, "a_better", "user", "synthetic")))
    return PreferenceData("manifest-v1", tuple(pairs), {})


def test_training_uses_only_frozen_train_and_evaluates_separate_holdouts(monkeypatch):
    data, seen = _data(), []
    original = cli.fit
    def spy(examples, **kwargs):
        seen.extend(examples)
        return original(examples, **kwargs)
    monkeypatch.setattr(cli, "fit", spy)
    model, report = cli.train_and_evaluate(data, allow_synthetic=True, epochs=2)
    assert [e.query.query_id for e in seen] == ["query-train"]
    assert report["training"]["input_pairs"] == 1
    assert set(report["evaluation"]) == {"validation", "test"}
    assert report["training_provenance"]["task_ids"] == ["task-train"]
    cli.verify_training_record(model, data, report)


@pytest.mark.parametrize("change", ["model", "manifest", "partition", "revision"])
def test_evaluation_refuses_mismatched_training_record(change):
    data = _data()
    model, report = cli.train_and_evaluate(data, allow_synthetic=True, epochs=2)
    if change == "model":
        report["model_revision"] = "old"
    elif change == "manifest":
        data = replace(data, manifest_id="different-manifest")
    elif change == "partition":
        report["training_provenance"]["partition"] = "test"
    else:
        data = replace(data, pairs=(replace(data.pairs[0], revision=2), *data.pairs[1:]))
    with pytest.raises(ValueError, match="训练记录"):
        cli.verify_training_record(model, data, report)


def test_new_holdout_labels_do_not_change_train_provenance():
    data = _data()
    model, report = cli.train_and_evaluate(data, allow_synthetic=True, epochs=2)
    changed = replace(data, pairs=(*data.pairs[:2], replace(data.pairs[2], revision=2)))
    cli.verify_training_record(model, changed, report)


def test_export_is_private_and_cannot_replace_existing_weight(tmp_path):
    path = tmp_path / "weights.json"
    cli.write_private(path, "synthetic-check")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cli.write_private(path, "replacement")
    assert path.read_text() == "synthetic-check"

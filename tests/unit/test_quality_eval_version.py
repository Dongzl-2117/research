"""题目、块号相同时，重切后的正文不得继承旧的金标准。"""

import json
import runpy
from pathlib import Path

import pytest

tool = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools/quality_eval/quality.py"))


def test_replaced_body_and_changed_range_require_relabelling():
    question = "回购"
    old = (1, 7, "董事会批准回购", 6, 8)
    labels = {tool["evidence_key"](question, *old): 2, "回购|1|7": 2}
    assert tool["view"](question, [old], labels)["ndcg"] == 1
    for changed in [(1, 7, "周末的食谱", 6, 8), (1, 7, old[2], 6, 9)]:
        assert tool["view"](question, [changed], labels)["unlabelled"] == 1


def test_new_questions_are_disjoint_and_pending_human_grades():
    queries = tool["QUERIES"]
    assert len(queries["new_hold"]) >= 5
    assert set(queries["new_hold"]).isdisjoint(queries["tune"] + queries["hold"])


@pytest.mark.parametrize(("marks", "coverage", "mean", "ndcg", "focus"), [
    ((None, None), "有评分 0/2 题，已判 0/2 条", None, None, None),
    ((2, None), "有评分 1/2 题，已判 1/2 条", "2.000", "1.000", "0.100"),
    ((0, None), "有评分 1/2 题，已判 1/2 条", "0.000", "0.000", "0.000"),
    ((2, 0), "有评分 2/2 题，已判 2/2 条", "1.000", "0.500", "0.050"),
])
def test_metrics_average_only_questions_with_a_current_judgment(marks, coverage, mean, ndcg, focus,
                                                                tmp_path, monkeypatch, capsys):
    data, labels = {}, {}
    for article, (query, grade) in enumerate(zip(tool["QUERIES"]["tune"][:2], marks, strict=True), 1):
        item = {"article_id": article, "chunk": 0, "text": "正文"}
        data[query] = {"chunks": [item], "fragments": [item]}
        if grade is not None:
            labels[tool["evidence_key"](query, article, 0, "正文")] = grade
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(data))
    monkeypatch.setitem(tool["metrics"].__globals__, "_labels", lambda *args: labels)
    tool["metrics"](snapshot)
    lines = capsys.readouterr().out.splitlines()
    empty_line = next(line for line in lines if "文章级" in line)
    assert "2 题无结果" in empty_line and "等待人工" not in empty_line
    quality_line = next(line for line in lines if "段落级" in line)
    focus_line = next(line for line in lines if "片段专门讲" in line)
    for line in (quality_line, focus_line):
        assert coverage in line
        if ndcg is None:
            assert "等待人工" in line and "ndcg=" not in line
        else:
            assert f"ndcg={ndcg}" in line
    if mean is not None:
        assert f"mean={mean}" in quality_line
        assert f"专门讲占比={focus}" in focus_line

"""题目、块号相同时，重切后的正文不得继承旧的金标准。"""

import runpy
from pathlib import Path

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

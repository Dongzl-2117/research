"""检索块「讲没讲机制」打分：分批、给模型的正文、读回答、和人工比。"""

import json

import pytest

from researchlib import mechanism
from researchlib.llm import LLMError


def _chunk(ord_, body, heading=""):
    return {"ord": ord_, "heading": heading, "body": body, "chunk_hash": f"h{ord_}"}


def test_batches_split_by_chars_and_keep_long_chunk_alone():
    chunks = [_chunk(1, "甲" * 2500), _chunk(2, "乙" * 2500), _chunk(3, "丙" * 9000), _chunk(4, "丁" * 100)]
    groups = mechanism.batches(chunks, limit=5500)
    # 超长的块按 CHUNK_LIMIT（3000）算字数：前两块 5000，加上第三块超了，第三块另起一批。
    assert [[c["ord"] for c in g] for g in groups] == [[1, 2], [3, 4]]
    assert len(mechanism.batches([_chunk(1, "甲" * 9000)], limit=1000)) == 1
    assert mechanism.batches([]) == []


def test_render_numbers_from_one_and_shows_heading():
    text = mechanism.render("某篇", [_chunk(20003, "第一段", "成本"), _chunk(20007, "第二段")])
    assert text.startswith("文章标题：某篇")
    assert "【1】（小标题：成本）\n第一段" in text and "【2】\n第二段" in text
    # 编号不用 ord，免得模型抄错上万的数。
    assert "20003" not in text


def test_parse_scores_reads_json_in_order_and_ignores_extra_text():
    text = '好的。\n{"scores": [{"id": 2, "score": 0}, {"id": 1, "score": 2}, {"id": 3, "score": "1"}]}'
    assert mechanism.parse_scores(text, 3) == [2, 0, 1]


@pytest.mark.parametrize(
    "text",
    [
        "没有 JSON",
        '{"scores": [{"id": 1, "score": 2}]}',  # 缺第 2 段
        '{"scores": [{"id": 1, "score": 3}, {"id": 2, "score": 1}]}',  # 分数越界
        '{"result": []}',
    ],
)
def test_parse_scores_rejects_incomplete_answers(text):
    with pytest.raises(LLMError):
        mechanism.parse_scores(text, 2)


def test_agreement_counts_exact_precision_and_kappa():
    perfect = mechanism.agreement([(2, 2), (1, 1), (0, 0), (2, 2)])
    assert perfect["exact"] == 1 and perfect["kappa"] == 1 and perfect["precision2"] == 1
    mixed = mechanism.agreement([(2, 2), (2, 0), (0, 2), (1, 1)])
    assert mixed["exact"] == 0.5 and mixed["precision2"] == 0.5 and mixed["recall2"] == 0.5
    assert mixed["matrix"][2][0] == 1 and mixed["kappa"] < 0.5
    assert mechanism.agreement([]) == {"n": 0}


def test_score_chunks_asks_once_per_batch(monkeypatch):
    asked = []

    def reply(_settings, messages, *, usage=None, **_kwargs):
        count = messages[1]["content"].count("【")
        asked.append(count)
        usage["prompt"] = usage.get("prompt", 0) + 100
        return json.dumps({"scores": [{"id": i, "score": i % 3} for i in range(1, count + 1)]}), "m"

    monkeypatch.setattr(mechanism, "chat", reply)
    usage = {}
    chunks = [_chunk(i, "字" * 2500) for i in range(5)]
    monkeypatch.setattr(mechanism, "BATCH_CHARS", 5000)
    scores, model = mechanism.score_chunks(None, "题", chunks, usage)
    assert asked == [2, 2, 1] and scores == [1, 2, 1, 2, 1] and model == "m" and usage["prompt"] == 300


def test_calibration_sample_is_well_formed():
    sample = mechanism.load_sample("tools/quality_eval/mechanism.json")
    assert len(sample) >= 300
    assert {item["label"] for item in sample} == {0, 1, 2}
    assert len({item["query"] for item in sample}) >= 7


def test_owner_sample_is_well_formed():
    sample = mechanism.load_sample("tools/quality_eval/mechanism_owner.json")
    assert len(sample) == 28
    assert {item["label"] for item in sample} == {0, 1, 2}
    assert len({(item["article_id"], item["ord"]) for item in sample}) == 28


def test_sample_can_come_from_stdin(monkeypatch):
    import io
    import json

    data = {"items": [{"query": "题", "article_id": 1, "ord": 2, "label": 1}]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    assert mechanism.load_sample("-") == [{"query": "题", "article_id": 1, "ord": 2, "label": 1}]


def test_owner_holdout_does_not_overlap_tuning_sample():
    tune = {(i["article_id"], i["ord"]) for i in mechanism.load_sample("tools/quality_eval/mechanism_owner.json")}
    hold = mechanism.load_sample("tools/quality_eval/mechanism_owner_holdout.json")
    assert len(hold) == 20 and not tune & {(i["article_id"], i["ord"]) for i in hold}

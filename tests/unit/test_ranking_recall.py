"""通用候选融合按快照身份合并；不依赖公司名或主题词表。"""
from dataclasses import replace

import pytest

from researchlib.ranking.contracts import RankCandidate, RankQuery, SourceFragment
from researchlib.ranking.recall import collect_hybrid


def candidate(query, name, entities=("e",)):
    return RankCandidate(query.query_id, "fixture", name, f"family:{name}", "v1", "合成标题", "",
                         (SourceFragment("center", 0, 0, 4, "原创文本", "chunk-v1"),), entities,
                         "test-only", "https://example.invalid/source")


class Provider:
    def __init__(self, name, candidates):
        self.name, self.candidates = name, candidates

    def collect(self, query, *, limit):
        return self.candidates[:limit]


def test_lexical_semantic_fusion_keeps_both_and_marks_channels():
    query = RankQuery("q", "虚构查询", "主题", ("e",), "{}")
    a, b, c = [candidate(query, name) for name in "abc"]
    result = collect_hybrid(query, [Provider("lexical", [a, a, b]), Provider("semantic", [c, b])],
                            eligible=lambda q, item: set(q.entity_ids) <= set(item.entity_ids))
    assert result[0].article_id == "b"
    assert set(result[0].recall_channels) == {"lexical", "semantic"}
    assert {item.article_id for item in result} == {"a", "b", "c"}


def test_fusion_never_uses_semantic_score_to_bypass_query_or_entity_scope():
    query = RankQuery("q", "虚构查询", "主题", ("e",), "{}")
    valid = candidate(query, "a")
    foreign = replace(candidate(query, "foreign", ("other",)), semantic_score=9999)
    wrong_query = replace(candidate(query, "old-query"), query_id="old")
    result = collect_hybrid(query, [Provider("semantic", [foreign, wrong_query, valid])],
                            eligible=lambda q, item: set(q.entity_ids) <= set(item.entity_ids))
    assert [item.article_id for item in result] == ["a"]


def test_reused_opaque_identity_cannot_point_to_two_versions():
    query = RankQuery("q", "虚构查询", "主题", (), "{}")
    first = replace(candidate(query, "a"), opaque_id="schema-validated-id")
    changed = replace(first, source_version="v2")
    with pytest.raises(ValueError, match="不同原文版本"):
        collect_hybrid(query, [Provider("lexical", [first]), Provider("semantic", [changed])],
                       eligible=lambda q, item: True)


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_fusion_is_bounded(limit):
    query = RankQuery("q", "虚构查询", "主题", (), "{}")
    with pytest.raises(ValueError):
        collect_hybrid(query, [], eligible=lambda q, item: True, limit=limit)

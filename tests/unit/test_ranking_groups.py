"""只用合成 task.data 验证分组审计；不调用 UI parser、模型、数据库或网络。"""
import json
from copy import deepcopy

import pytest

from researchlib.ranking import groupaudit
from researchlib.ranking.groupaudit import audit_groups

LONG_TEXT = (
    "这份研究记录了沿海工厂的经营变化。新增生产线已经完成试运行，管理层预计下半年开始贡献现金流。"
    "本次调研覆盖零部件采购、运输费用、渠道库存、客户回款和售后服务。不同地区经销商提供了独立的订单数据，"
    "其中东部地区的客户续费保持稳定，西部地区仍在调整销售策略。研究员同时核对了主要供应商的交货周期，"
    "发现原料备货比去年充足，运输拥堵已有缓解，但劳动力成本继续上升。未来需要观察产能利用率和经营现金回收速度。"
)


def _task(name, *, group=None, split="train", source="source-one", material="synthetic"):
    candidates = []
    for side in ("a", "b"):
        candidates.append({
            "candidate_id": f"candidate-{name}-{side}", "article_id": f"article-{name}-{side}",
            "chunk_id": 0, "window_id": f"window-{name}-{side}",
            "document_family_id": f"family-{name}-{side}", "content_version": "v1",
            "content_hash": f"hash-{name}-{side}", "source_id": source,
            "article_title": f"合成文章{name}{side}", "heading": "经营资料",
            "text": f"合成片段{name}{side}说明独立的经营资料和调研结论。",
            "context_before": "", "context_after": "", "source_url": "https://example.invalid/synthetic",
            "algorithm": {"name": "synthetic-demo"},
        })
    return {
        "task": {"query": f"研究{name}的经营情况", "query_id": f"query-{name}", "scope": {},
                 "entity_ids": [f"entity-{name}"], "topic_ids": [f"topic-{name}"],
                 "task_id": f"task-{name}", "material": material, "candidates": candidates},
        "group_id": group or f"group-{name}", "split": split, "manifest_id": "ui-frozen-manifest",
    }


def _candidate(task, index=0):
    return task["task"]["candidates"][index]


def test_different_companies_sharing_a_topic_cannot_cross_splits():
    first, second = _task("青禾"), _task("远帆", split="test")
    first["task"]["topic_ids"] = second["task"]["topic_ids"] = ["topic-pricing-power"]
    with pytest.raises(ValueError, match="共享主题.*不能随机拆分"):
        audit_groups([first, second])


def test_near_copy_across_documents_is_joined_with_explainable_similarity():
    first, second = _task("one"), _task("two", split="validation")
    _candidate(first)["text"] = LONG_TEXT
    _candidate(second)["text"] = LONG_TEXT.replace("现金流", "现金量", 1)
    with pytest.raises(ValueError, match="5-shingle Jaccard="):
        audit_groups([first, second])


def test_exact_body_does_not_depend_on_title_version_or_context():
    first, second = _task("one"), _task("two", split="test")
    _candidate(first)["text"] = "ＡＢＣ产品渠道库存与经营现金回款情况。"
    _candidate(second)["text"] = "abc 产品：渠道库存与经营现金回款情况！"
    _candidate(second)["article_title"] = "完全不同的标题"
    _candidate(second)["content_version"] = "v2"
    _candidate(second)["context_before"] = "这一版新增了额外的前文。"
    with pytest.raises(ValueError, match="原文正文.*与标题无关"):
        audit_groups([first, second])


def test_near_duplicate_full_windows_include_provided_context():
    first, second = _task("one"), _task("two", split="test")
    _candidate(first)["text"] = "第一种结论说明经营总体平稳。"
    _candidate(second)["text"] = "第二种结论说明经营总体平稳。"
    _candidate(first)["context_before"] = _candidate(second)["context_before"] = LONG_TEXT
    with pytest.raises(ValueError, match="5-shingle Jaccard="):
        audit_groups([first, second])


def test_same_article_with_new_body_and_version_cannot_cross_splits():
    first, second = _task("one"), _task("two", split="test")
    _candidate(second)["article_id"] = _candidate(first)["article_id"]
    _candidate(second)["content_version"] = "version-two"
    with pytest.raises(ValueError, match="同来源同 article_id，忽略版本差异"):
        audit_groups([first, second])


@pytest.mark.parametrize("field", ["query_id", "entity_ids"])
def test_shared_query_or_entity_cannot_be_assigned_to_different_groups(field):
    first, second = _task("one"), _task("two")
    second["task"][field] = first["task"][field]
    with pytest.raises(ValueError, match="同一连通分量跨 group_id"):
        audit_groups([first, second])


@pytest.mark.parametrize("field", ["document_family_id", "content_hash", "candidate_id"])
def test_shared_document_or_content_identity_cannot_cross_splits(field):
    first, second = _task("one"), _task("two", split="validation")
    _candidate(second)[field] = _candidate(first)[field]
    with pytest.raises(ValueError, match=f"相同 {field}"):
        audit_groups([first, second])


def test_new_bridge_reconnects_two_previously_independent_frozen_groups():
    first, second = _task("one"), _task("two")
    assert audit_groups([first, second])["component_count"] == 2
    bridge = _task("bridge")
    bridge["task"]["entity_ids"] = first["task"]["entity_ids"]
    bridge["task"]["topic_ids"] = second["task"]["topic_ids"]
    with pytest.raises(ValueError, match="同一连通分量跨 group_id"):
        audit_groups([first, second, bridge])


def test_one_component_is_not_randomly_split_or_mutated():
    first, second = _task("one", group="all"), _task("two", group="all")
    second["task"]["entity_ids"] = first["task"]["entity_ids"]
    original = deepcopy([first, second])
    report = audit_groups([first, second])
    assert [first, second] == original
    assert report["component_count"] == 1 and report["component_sizes"] == [2]
    assert not report["evaluable"]
    assert any("只有一个连通分量" in reason for reason in report["not_evaluable_reasons"])
    assert any("validation 没有任务" in reason for reason in report["not_evaluable_reasons"])
    assert any("test 没有任务" in reason for reason in report["not_evaluable_reasons"])


def test_three_cold_groups_report_coverage_without_claiming_real_generalization():
    tasks = [_task("train"), _task("validation", split="validation"), _task("test", split="test")]
    report = audit_groups(tasks)
    assert report["manifest_id"] == "ui-frozen-manifest"
    assert report["evaluable"] and report["not_evaluable_reasons"] == []
    assert report["task_count"] == report["group_count"] == report["component_count"] == 3
    assert report["component_sizes"] == [1, 1, 1]
    for split in ("train", "validation", "test"):
        summary = report["splits"][split]
        assert summary["tasks"] == summary["groups"] == summary["entities"] == summary["topics"] == 1
        assert summary["sources"] == 1 and summary["candidates"] == 2
    for split in ("validation", "test"):
        cold = report["cold_start"][split]
        assert cold["joint_entity_topic_cold_start"] and cold["entity_coverage_complete"]
        assert cold["entity_overlap_with_other_splits"] == cold["topic_overlap_with_other_splits"] == []
    assert report["material_counts"] == {"synthetic": 3, "private_corpus": 0}
    assert any("合成分布" in line and "不能声称真实泛化" in line for line in report["limitations"])
    assert json.loads(json.dumps(report, ensure_ascii=False)) == report
    assert audit_groups(list(reversed(tasks))) == report


def test_multiple_independent_components_may_share_one_frozen_group():
    tasks = [_task("one", group="merged"), _task("two", group="merged"),
             _task("validation", split="validation"), _task("test", split="test")]
    report = audit_groups(tasks)
    assert report["evaluable"] and report["component_count"] == 4 and report["group_count"] == 3
    assert report["splits"]["train"]["tasks"] == 2 and report["splits"]["train"]["groups"] == 1


def test_even_disconnected_components_of_one_group_cannot_cross_splits():
    with pytest.raises(ValueError, match="冻结组不能跨划分"):
        audit_groups([_task("one", group="shared"), _task("two", group="shared", split="test")])


def test_unknown_source_conservatively_links_all_same_numbered_articles():
    first, second, unknown = _task("one", source="source-a"), _task("two", source="source-b"), _task("unknown")
    _candidate(second)["article_id"] = _candidate(first)["article_id"]
    assert audit_groups([first, second])["component_count"] == 2
    _candidate(unknown)["article_id"] = _candidate(first)["article_id"]
    del _candidate(unknown)["source_id"]
    with pytest.raises(ValueError, match="未知来源，保守连接"):
        audit_groups([first, second, unknown])


@pytest.mark.parametrize("missing", [None, "", "unknown"])
def test_missing_source_is_counted_without_guessing_from_algorithm_scope_or_url(missing):
    task = _task("unknown")
    for candidate in task["task"]["candidates"]:
        candidate["source_id"] = missing
    task["task"]["scope"] = {"source_ids": ["source-a", "source-b"]}
    report = audit_groups([task])
    assert report["missing_source_id_count"] == 2
    assert report["splits"]["train"]["sources"] == 0
    assert report["splits"]["train"]["source_ids"] == []


def test_ui_normalized_unknown_source_conservatively_links_known_source_article():
    first, second = _task("one", source="source-a"), _task("two", source="unknown", split="test")
    _candidate(second)["article_id"] = _candidate(first)["article_id"]
    with pytest.raises(ValueError, match="未知来源，保守连接"):
        audit_groups([first, second])


def test_synthetic_and_private_material_counts_remain_separate():
    # private_corpus 在此只是元数据分支的合成测试，样本不是真实用户材料。
    tasks = [_task("one", material="private_corpus"),
             _task("two", split="validation"), _task("three", split="test", material="private_corpus")]
    report = audit_groups(tasks)
    assert report["material_counts"] == {"synthetic": 1, "private_corpus": 2}
    assert report["splits"]["validation"]["material_counts"] == {"synthetic": 1, "private_corpus": 0}


@pytest.mark.parametrize("topic_ids", [None, [], "unknown"])
def test_missing_topic_metadata_cannot_claim_leakage_protection(topic_ids):
    task = _task("one")
    task["task"]["topic_ids"] = topic_ids
    with pytest.raises(ValueError, match="topic_ids"):
        audit_groups([task])


@pytest.mark.parametrize("field", ["document_family_id", "article_id", "content_hash", "content_version"])
def test_missing_document_identity_is_rejected_instead_of_guessed(field):
    task = _task("one")
    _candidate(task).pop(field)
    with pytest.raises(ValueError, match=field):
        audit_groups([task])


def test_entity_free_tasks_do_not_look_like_verified_entity_cold_start():
    tasks = [_task("train"), _task("validation", split="validation"), _task("test", split="test")]
    tasks[-1]["task"]["entity_ids"] = []
    report = audit_groups(tasks)
    assert not report["evaluable"]
    assert not report["cold_start"]["test"]["entity_coverage_complete"]
    assert not report["cold_start"]["test"]["joint_entity_topic_cold_start"]


def test_missing_training_entity_metadata_prevents_both_cold_start_claims():
    tasks = [_task("train"), _task("validation", split="validation"), _task("test", split="test")]
    tasks[0]["task"]["entity_ids"] = []
    report = audit_groups(tasks)
    assert not report["evaluable"]
    for heldout in ("validation", "test"):
        assert not report["cold_start"][heldout]["reference_entity_coverage_complete"]
        assert not report["cold_start"][heldout]["joint_entity_topic_cold_start"]


def test_empty_input_and_mixed_or_duplicate_ui_identities():
    report = audit_groups([])
    assert not report["evaluable"] and report["component_count"] == 0
    first, second = _task("one"), _task("two")
    second["manifest_id"] = "another-manifest"
    with pytest.raises(ValueError, match="不同的冻结 manifest_id"):
        audit_groups([first, second])
    with pytest.raises(ValueError, match="task_id 重复"):
        audit_groups([first, deepcopy(first)])
    with pytest.raises(ValueError, match="UI parser"):
        audit_groups([{"schema_version": 1, "record_type": "task", "data": first}])


@pytest.mark.parametrize("limit", ["MAX_TASKS", "MAX_TEXT_CHARS", "MAX_TOTAL_TEXT_CHARS", "MAX_TOTAL_SHINGLES",
                                       "MAX_NEAR_COMPARISONS"])
def test_resource_limits_reject_audit_without_sampling_or_truncating(monkeypatch, limit):
    first, second = _task("one"), _task("two")
    _candidate(first)["text"] = LONG_TEXT
    _candidate(second)["text"] = LONG_TEXT.replace("现金流", "现金量", 1)
    before = deepcopy([first, second])
    monkeypatch.setattr(groupaudit, limit, 0)
    with pytest.raises(ValueError, match="审计上限"):
        audit_groups([first, second])
    assert [first, second] == before

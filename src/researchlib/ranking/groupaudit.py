"""只读审计 UI schema v1 的 task.data；验证冻结分组，不生成或改写 group/split。

调用方须先使用 UI 的权威 parser 校验 envelope、候选快照及其 0.85+containment 近重复规则。
本模块的 0.90 字符 shingle 是补充检查，不能替换或放宽 UI 规则。本模块只验证防泄漏所需的元数据，
按实体、主题、查询、原稿、文档族和原文重复关系取传递闭包。审计通过只表示这些可观测关系没有跨划分。
"""
from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

SPLITS = ("train", "validation", "test")
MATERIALS = ("synthetic", "private_corpus")
SHINGLE_WIDTH = 5
NEAR_DUPLICATE_THRESHOLD = 0.90
MIN_NEAR_TEXT_CHARS = 80
# 超限时拒绝审计，不能截断、随机抽样后继续声称无泄漏。
MAX_TASKS = 10000
MAX_TEXT_CHARS = 100000
MAX_TOTAL_TEXT_CHARS = 5000000
MAX_TOTAL_SHINGLES = 1000000
MAX_NEAR_COMPARISONS = 500000


def _identifier(value, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int) or not str(value).strip():
        raise ValueError(f"{field} 缺失或无效，不能进行防泄漏审计")
    return str(value).strip()


def _ids(value, field: str, *, required: bool = False) -> frozenset[str]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{field} 必须是明确的 ID 列表，不能猜测实体或主题")
    found = frozenset(_identifier(item, field) for item in value)
    if required and not found:
        raise ValueError(f"{field} 为空，缺少主题族，不能声称可防止主题泄漏")
    return found


def _plain(value, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{field} 缺失或无效，不能审计原文重复关系")
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError(f"{field} 超过原文审计上限 {MAX_TEXT_CHARS} 字符；未截断或抽样")
    # 标题不参与正文身份；保留数字、汉字和字母，忽略大小写、全半角、空白和标点差异。
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())


def _parse(tasks: Sequence[dict]) -> list[dict]:
    if isinstance(tasks, str | bytes | Mapping) or not isinstance(tasks, Sequence):
        raise ValueError("audit_groups 只接收 UI parser 已解析的 task.data 字典序列")
    if len(tasks) > MAX_TASKS:
        raise ValueError(f"任务数超过审计上限 {MAX_TASKS}；不能用抽样代替完整防泄漏审计")
    found, seen = [], set()
    total_text_chars = 0
    for index, data in enumerate(tasks):
        prefix = f"第 {index + 1} 项"
        if not isinstance(data, Mapping) or not isinstance(data.get("task"), Mapping):
            raise ValueError(f"{prefix} 不是 task.data，请先使用 UI parser 读取权威 envelope")
        task = data["task"]
        task_id = _identifier(task.get("task_id"), f"{prefix}.task_id")
        if task_id in seen:
            raise ValueError(f"task_id 重复：{task_id}；审计输入应为去重后的任务快照")
        seen.add(task_id)
        group = _identifier(data.get("group_id"), f"{task_id}.group_id")
        split = data.get("split")
        if split not in SPLITS:
            raise ValueError(f"{task_id}.split 必须是 train、validation 或 test")
        material = task.get("material")
        if material not in MATERIALS:
            raise ValueError(f"{task_id}.material 必须区分 synthetic 与 private_corpus")
        candidates = task.get("candidates")
        if not isinstance(candidates, list | tuple) or len(candidates) != 2:
            raise ValueError(f"{task_id} 必须保留两个候选快照")
        parsed_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise ValueError(f"{task_id} 的候选必须是字典")
            total_text_chars += sum(
                len(candidate[name]) for name in ("text", "context_before", "context_after")
                if isinstance(candidate.get(name), str)
            )
            if total_text_chars > MAX_TOTAL_TEXT_CHARS:
                raise ValueError(f"原文总长度超过审计上限 {MAX_TOTAL_TEXT_CHARS} 字符；未截断或抽样")
            parsed = {
                name: _identifier(candidate.get(name), f"{task_id}.{name}")
                for name in ("article_id", "document_family_id", "content_hash", "content_version", "candidate_id")
            }
            source = candidate.get("source_id")
            parsed["source_id"] = (
                _identifier(source, f"{task_id}.source_id") if source not in (None, "", "unknown") else None
            )
            parsed["text"] = _plain(candidate.get("text"), f"{task_id}.text", required=True)
            if not parsed["text"]:
                raise ValueError(f"{task_id}.text 没有可比较的原文字符")
            before = _plain(candidate.get("context_before", ""), f"{task_id}.context_before")
            after = _plain(candidate.get("context_after", ""), f"{task_id}.context_after")
            parsed["window_text"] = before + parsed["text"] + after
            parsed_candidates.append(parsed)
        found.append({
            "task_id": task_id, "query_id": _identifier(task.get("query_id"), f"{task_id}.query_id"),
            "group_id": group, "split": split, "material": material,
            "manifest_id": _identifier(data.get("manifest_id"), f"{task_id}.manifest_id"),
            "entity_ids": _ids(task.get("entity_ids"), f"{task_id}.entity_ids"),
            "topic_ids": _ids(task.get("topic_ids"), f"{task_id}.topic_ids", required=True),
            "candidates": parsed_candidates,
        })
    manifests = {task["manifest_id"] for task in found}
    if len(manifests) > 1:
        raise ValueError(f"不能混合不同的冻结 manifest_id：{sorted(manifests)}")
    return sorted(found, key=lambda task: task["task_id"])


def _summary(records: list[dict], indices: list[int]) -> dict:
    selected = [records[index] for index in indices]
    entities = {entity for task in selected for entity in task["entity_ids"]}
    topics = {topic for task in selected for topic in task["topic_ids"]}
    sources = {c["source_id"] for task in selected for c in task["candidates"] if c["source_id"] is not None}
    material_counts = Counter(task["material"] for task in selected)
    return {
        "tasks": len(selected), "candidates": sum(len(task["candidates"]) for task in selected),
        "groups": len({task["group_id"] for task in selected}),
        "entities": len(entities), "entity_ids": sorted(entities),
        "topics": len(topics), "topic_ids": sorted(topics),
        "sources": len(sources), "source_ids": sorted(sources),
        "missing_source_id_count": sum(c["source_id"] is None for task in selected for c in task["candidates"]),
        "entity_covered_tasks": sum(bool(task["entity_ids"]) for task in selected),
        "material_counts": {kind: material_counts[kind] for kind in MATERIALS},
    }


def audit_groups(tasks: Sequence[dict]) -> dict:
    """审计已冻结的 task.data，返回可序列化报告；缺元数据、资源超限或组件跨组/划分时抛 ValueError。

    多个独立组件可以属于同一 group，但一个 group 不能跨 split。source_id 未知时，相同 article_id
    与所有同号文章保守相连；来源完整时文章身份为 (source_id, article_id)，不从 URL 或算法名猜来源。
    """
    records = _parse(tasks)
    parents = list(range(len(records)))
    witnesses: list[dict] = []
    counts: Counter = Counter()

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def connect(left: int, right: int, relation: str, evidence: str) -> None:
        if left == right:
            return
        counts[relation] += 1
        a, b = root(left), root(right)
        if a != b:
            parents[max(a, b)] = min(a, b)
            witnesses.append({
                "left_task_id": records[left]["task_id"], "right_task_id": records[right]["task_id"],
                "relation": relation, "evidence": evidence,
            })

    owners: dict[tuple, int] = {}
    texts: dict[str, int] = {}
    articles: dict[str, list[tuple[int, str | None]]] = defaultdict(list)

    def key(index: int, relation: str, value, evidence: str = "") -> None:
        lookup = relation, value
        if lookup in owners:
            connect(owners[lookup], index, relation, evidence or relation)
        else:
            owners[lookup] = index

    for index, task in enumerate(records):
        key(index, "query_id", task["query_id"], "相同 query_id")
        for entity in sorted(task["entity_ids"]):
            key(index, "entity_id", entity, f"共享实体 {entity}")
        for topic in sorted(task["topic_ids"]):
            key(index, "topic_id", topic, f"共享主题 {topic}")
        for candidate in task["candidates"]:
            articles[candidate["article_id"]].append((index, candidate["source_id"]))
            key(index, "document_family_id", candidate["document_family_id"], "相同 document_family_id")
            key(index, "content_hash", candidate["content_hash"], "相同 content_hash")
            key(index, "candidate_id", candidate["candidate_id"], "相同 candidate_id")
            for body in dict.fromkeys((candidate["text"], candidate["window_text"])):
                if body in texts:
                    connect(texts[body], index, "exact_text", "原文正文或完整上下文规范化后完全相同，与标题无关")
                else:
                    texts[body] = index
    for article_id, members in sorted(articles.items()):
        if any(source is None for _index, source in members):
            first = members[0][0]
            for index, _source in members[1:]:
                connect(first, index, "article_id_unknown_source", f"article_id={article_id} 存在未知来源，保守连接")
        else:
            for index, source in members:
                key(index, "article_id", (source, article_id), "同来源同 article_id，忽略版本差异")

    signatures = []
    shingle_count = 0
    for body, owner in texts.items():
        if len(body) < MIN_NEAR_TEXT_CHARS:
            continue
        shingles = frozenset(body[start : start + SHINGLE_WIDTH] for start in range(len(body) - SHINGLE_WIDTH + 1))
        shingle_count += len(shingles)
        if shingle_count > MAX_TOTAL_SHINGLES:
            raise ValueError(f"字符 shingle 总数超过审计上限 {MAX_TOTAL_SHINGLES}；未截断或随机采样")
        signatures.append((len(shingles), owner, shingles))
    signatures.sort(key=lambda item: (item[0], item[1]))
    if len({root(owner) for _size, owner, _shingles in signatures}) < 2:
        signatures = []
    comparisons = 0
    visited_pairs = 0
    for pos, (size, owner, shingles) in enumerate(signatures):
        for other_pos in range(pos + 1, len(signatures)):
            other_size, other_owner, other = signatures[other_pos]
            # Jaccard 的最大可能值也不足 0.90 时无需求交集。整数比较避免阈值边界舍入。
            if size * 10 < other_size * 9:
                break
            visited_pairs += 1
            if visited_pairs > MAX_NEAR_COMPARISONS:
                raise ValueError(f"近重复候选对超过审计上限 {MAX_NEAR_COMPARISONS}；不能声称完整防泄漏")
            if owner == other_owner or root(owner) == root(other_owner):
                continue
            comparisons += 1
            overlap = len(shingles & other)
            union = size + other_size - overlap
            if overlap * 10 >= union * 9:
                connect(
                    owner, other_owner, "near_duplicate", f"字符 {SHINGLE_WIDTH}-shingle Jaccard={overlap / union:.3f}"
                )

    buckets: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        buckets[root(index)].append(index)
    components = []
    for indices in buckets.values():
        groups = sorted({records[i]["group_id"] for i in indices})
        splits = sorted({records[i]["split"] for i in indices})
        if len(groups) > 1 or len(splits) > 1:
            ids = [records[i]["task_id"] for i in indices]
            links = [edge for edge in witnesses if edge["left_task_id"] in ids and edge["right_task_id"] in ids]
            reasons = list(dict.fromkeys(edge["evidence"] for edge in links))[:5]
            raise ValueError(
                f"防泄漏审计失败：同一连通分量跨 group_id={groups} 或 split={splits}；"
                f"task_ids={ids[:10]}；连接依据：{'；'.join(reasons)}。必须修正冻结分组，不能随机拆分组件"
            )
        components.append({"task_ids": [records[i]["task_id"] for i in indices],
                           "group_id": groups[0], "split": splits[0], **_summary(records, indices)})
    group_splits: dict[str, set[str]] = defaultdict(set)
    for task in records:
        group_splits[task["group_id"]].add(task["split"])
    for group, splits in sorted(group_splits.items()):
        if len(splits) > 1:
            raise ValueError(f"group_id={group} 的独立组件被分配到多个 split={sorted(splits)}；冻结组不能跨划分")

    splits = {split: _summary(records, [i for i, task in enumerate(records) if task["split"] == split])
              for split in SPLITS}
    reasons = []
    if not records:
        reasons.append("没有任务，无法评估")
    elif len(components) == 1:
        reasons.append("全部任务只有一个连通分量，无法形成独立 holdout；不能随机拆分")
    for split, summary in splits.items():
        if not summary["tasks"]:
            reasons.append(f"{split} 没有任务，无法评估该划分")
    missing_entities = sum(not task["entity_ids"] for task in records)
    if missing_entities:
        reasons.append(f"{missing_entities} 个任务缺少实体覆盖，不能核对完整的实体冷启动")
    cold_start = {}
    for split in ("validation", "test"):
        summary = splits[split]
        other_entities = {value for other in SPLITS if other != split for value in splits[other]["entity_ids"]}
        other_topics = {value for other in SPLITS if other != split for value in splits[other]["topic_ids"]}
        entity_overlap = sorted(set(summary["entity_ids"]) & other_entities)
        topic_overlap = sorted(set(summary["topic_ids"]) & other_topics)
        complete = bool(summary["tasks"]) and summary["entity_covered_tasks"] == summary["tasks"]
        reference_complete = bool(splits["train"]["tasks"]) and all(
            splits[other]["entity_covered_tasks"] == splits[other]["tasks"]
            for other in SPLITS if other != split
        )
        cold_start[split] = {
            "tasks": summary["tasks"], "entity_coverage_complete": complete,
            "reference_entity_coverage_complete": reference_complete,
            "topic_coverage_complete": bool(summary["tasks"]),
            "entity_overlap_with_other_splits": entity_overlap, "topic_overlap_with_other_splits": topic_overlap,
            "joint_entity_topic_cold_start": bool(reference_complete and complete
                                                  and not entity_overlap and not topic_overlap),
        }
    total = _summary(records, list(range(len(records))))
    return {
        "manifest_id": records[0]["manifest_id"] if records else None,
        "task_count": len(records), "group_count": total["groups"], "component_count": len(components),
        "component_sizes": sorted((component["tasks"] for component in components), reverse=True),
        "components": components, "splits": splits, "cold_start": cold_start,
        "material_counts": total["material_counts"], "missing_source_id_count": total["missing_source_id_count"],
        "connections": dict(sorted(counts.items())), "connection_witnesses": witnesses,
        "evaluable": not reasons, "not_evaluable_reasons": reasons,
        "near_duplicate_policy": {
            "role": "supplementary_to_ui_parser",
            "shingle_width": SHINGLE_WIDTH, "jaccard_threshold": NEAR_DUPLICATE_THRESHOLD,
            "minimum_chars": MIN_NEAR_TEXT_CHARS, "compared_pairs": comparisons, "visited_pairs": visited_pairs,
            "max_tasks": MAX_TASKS, "max_text_chars": MAX_TEXT_CHARS,
            "max_total_text_chars": MAX_TOTAL_TEXT_CHARS,
            "max_total_shingles": MAX_TOTAL_SHINGLES, "max_comparisons": MAX_NEAR_COMPARISONS,
        },
        "limitations": [
            "evaluable 只表示冻结划分在已提供元数据下结构可评估，不证明标签可靠、模型效果或真实泛化。",
            "synthetic 与 private_corpus 独立计数；合成分布的成绩只能验证程序流程，不能声称真实泛化。",
            "近重复只核对规范化后的高相似字符 shingle；短于门槛的原文只比较精确重复，不保证语义改写均被识别。",
            "0.90 字符 shingle 仅补充 UI 权威 parser 的 0.85+containment 校验，不能代替或放宽它。",
            "缺 source_id 的候选计入未知来源，相同 article_id 保守连接；来源数不包含未知来源。",
        ],
    }

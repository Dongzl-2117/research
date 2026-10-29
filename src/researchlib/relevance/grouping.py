"""冻结实体、主题、同文档族与近重复的连通分组；增量输入不移动已有分组。"""

from __future__ import annotations

import unicodedata

from researchlib.relevance.schema import FrozenTask, Manifest, Task, digest


def group_keys(task: Task) -> set[str]:
    return (
        {f"entity:{key}" for key in task.entity_ids}
        | {f"topic:{key}" for key in task.topic_ids}
        | {f"article:{c.article_id}" for c in task.candidates}
        | {f"family:{c.document_family_id}" for c in task.candidates}
        | {f"content:{c.content_hash}" for c in task.candidates}
        | {f"query:{task.query_id}"}
    )


def text_fingerprint(text: str) -> frozenset[str]:
    text = "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())
    return frozenset(text[i : i + 5] for i in range(max(1, len(text) - 4)))


def related(left: Task, right: Task) -> bool:
    if group_keys(left) & group_keys(right):
        return True
    # 同文不同来源、标点/空白变化和高度重叠窗口，不能跨集合。
    for a in left.candidates:
        for b in right.candidates:
            fa, fb = text_fingerprint(a.text), text_fingerprint(b.text)
            overlap = len(fa & fb)
            if (
                fa == fb
                or overlap / max(1, len(fa | fb)) >= 0.85
                or min(len(fa), len(fb)) >= 20
                and overlap / min(len(fa), len(fb)) >= 0.9
            ):
                return True
    return False


def freeze(existing: list[FrozenTask], pending: list[Task], manifest: Manifest) -> list[FrozenTask]:
    """连到多个已冻结组就拒绝；不能因新数据到来重新划分测试集。"""
    unique = {item.task.task_id: item for item in existing}
    tasks = {}
    for task in pending:
        old = unique.get(task.task_id)
        other = tasks.get(task.task_id)
        if old and old.task != task or other and other != task:
            raise ValueError("相同 query/证据对已有不同元数据，需新内容版本")
        if not old:
            tasks[task.task_id] = task
    components: list[list[Task]] = []
    for task in sorted(tasks.values(), key=lambda t: t.task_id):
        matches = [group for group in components if any(related(task, member) for member in group)]
        merged = [task]
        for group in matches:
            merged.extend(group)
            components.remove(group)
        components.append(merged)
    result = list(existing)
    for group in components:
        linked = {item.group_id: item.split for item in existing if any(related(item.task, t) for t in group)}
        if len(linked) > 1:
            raise ValueError("新任务连接多个已冻结分组；拒绝导入，须在新数据集人工复核文档族/分组")
        if linked:
            group_id, split = next(iter(linked.items()))
        else:
            group_id = digest(sorted({key for task in group for key in group_keys(task)}))
            bucket = int(digest([manifest.seed, group_id])[:8], 16) % 100
            split = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
        result.extend(
            FrozenTask(task=t, group_id=group_id, split=split, manifest_id=manifest.manifest_id) for t in group
        )
    return sorted(result, key=lambda item: item.task.task_id)


def validate_frozen(tasks: list[FrozenTask], manifest: Manifest) -> None:
    groups = {}
    for index, item in enumerate(tasks):
        if item.manifest_id != manifest.manifest_id:
            raise ValueError("任务的 manifest 不符")
        old_split = groups.setdefault(item.group_id, item.split)
        if old_split != item.split:
            raise ValueError("冻结组跨 split")
        for other in tasks[:index]:
            if related(item.task, other.task) and item.group_id != other.group_id:
                raise ValueError("实体/主题/文档族/近重复跨冻结分组")

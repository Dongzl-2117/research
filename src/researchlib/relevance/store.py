"""仅显式打开的独立 SQLite 记录库；事务串行化，修改/撤销均保留事件。"""

from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from researchlib.relevance.grouping import freeze, validate_frozen
from researchlib.relevance.schema import (
    FrozenTask,
    LabelEvent,
    LabelRequest,
    LabelSource,
    Manifest,
    Model,
    Presentation,
    Task,
    canonical,
)

TYPES = {"manifest": Manifest, "task": FrozenTask, "presentation": Presentation, "label_event": LabelEvent}


class Conflict(ValueError):
    """请求重复但内容不同，或另一标签页已经改过版本。"""


class Record(Model):
    schema_version: Literal[1] = 1
    record_type: Literal["manifest", "task", "presentation", "label_event"]
    data: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_data(self):
        parsed = TYPES[self.record_type].model_validate(self.data)
        object.__setattr__(self, "data", parsed.model_dump(mode="json"))
        return self


def record(kind: str, value: Model) -> Record:
    return Record(record_type=kind, data=value.model_dump(mode="json"))


def record_id(item: Record) -> str:
    return (
        item.data[
            {"manifest": "manifest_id", "presentation": "presentation_id", "label_event": "event_id"}[item.record_type]
        ]
        if item.record_type != "task"
        else item.data["task"]["task_id"]
    )


@dataclass
class Dataset:
    manifest: Manifest
    tasks: list[FrozenTask]
    presentations: list[Presentation]
    events: list[LabelEvent]

    def current(self, task_id: str, source: LabelSource) -> LabelEvent | None:
        rows = [
            e
            for e in self.events
            if e.task_id == task_id and e.source.kind == source.kind and e.source.labeler_id == source.labeler_id
        ]
        return max(rows, key=lambda e: e.revision) if rows else None

    def report(self) -> dict:
        """派生覆盖报告，不改 manifest 或冻结边界，也不把弱标签算成人类真值。"""
        splits = ("train", "validation", "test")
        task_counts = Counter(f.split for f in self.tasks)
        groups = {f.group_id: f.split for f in self.tasks}
        group_sizes = Counter(f.group_id for f in self.tasks)
        group_counts = Counter(groups.values())
        latest = {}
        for event in self.events:
            key = (event.source.kind, event.source.labeler_id, event.task_id)
            if key not in latest or event.revision > latest[key].revision:
                latest[key] = event
        directional = Counter(
            e.split for e in latest.values() if e.source.kind == "human" and e.decision in {"left", "right"}
        )
        warnings = []
        if self.tasks and len(groups) == 1:
            warnings.append(
                {"code": "single_connected_group", "message": "全部任务坍缩到同一连通组，无法构成独立 holdout。"}
            )
        largest_share = max(group_sizes.values(), default=0) / max(1, len(self.tasks))
        if len(groups) > 1 and largest_share >= 0.8:
            warnings.append({"code": "dominant_group", "message": "最大冻结组覆盖至少 80% 任务，分组覆盖不足。"})
        missing = [split for split in splits if not task_counts[split]]
        if missing:
            warnings.append({"code": "missing_task_splits", "message": "缺少任务集合：" + ", ".join(missing)})
        if not directional["validation"] or not directional["test"]:
            warnings.append(
                {
                    "code": "no_human_directional_holdout",
                    "message": "验证集或测试集尚无人类方向偏好标签，不能据训练集成绩宣称泛化。",
                }
            )
        if self.tasks and all(f.task.material == "synthetic" for f in self.tasks):
            warnings.append({"code": "synthetic_only", "message": "全部是合成材料，只验证流程，不代表真实库效果。"})
        return {
            "schema_version": 1,
            "dataset_id": self.manifest.dataset_id,
            "manifest_id": self.manifest.manifest_id,
            "tasks": len(self.tasks),
            "groups": len(groups),
            "task_counts": {s: task_counts[s] for s in splits},
            "group_counts": {s: group_counts[s] for s in splits},
            "largest_group_share": largest_share,
            "human_directional_counts": {s: directional[s] for s in splits},
            "event_counts": {kind: sum(e.source.kind == kind for e in self.events) for kind in ("human", "ai_weak")},
            "warnings": warnings,
        }


def validate_records(records: list[Record]) -> Dataset:
    unique = {}
    for item in records:
        parsed = TYPES[item.record_type].model_validate(item.data)
        key = (item.record_type, record_id(record(item.record_type, parsed)))
        old = unique.setdefault(key, parsed)
        if old != parsed:
            raise ValueError("重复 ID 的记录内容不同")
    manifests = [value for (kind, _), value in unique.items() if kind == "manifest"]
    if len(manifests) != 1:
        raise ValueError("必须恰有一个冻结 manifest；不能混入另一数据集")
    dataset = Dataset(
        manifests[0],
        *[
            [value for (kind, _), value in unique.items() if kind == wanted]
            for wanted in ("task", "presentation", "label_event")
        ],
    )
    validate_frozen(dataset.tasks, dataset.manifest)
    tasks = {f.task.task_id: f for f in dataset.tasks}
    presentations = {p.presentation_id: p for p in dataset.presentations}
    actors = set()
    for p in dataset.presentations:
        frozen = tasks.get(p.task_id)
        if not frozen or {p.left_id, p.right_id} != {c.candidate_id for c in frozen.task.candidates}:
            raise ValueError("展示左右映射与任务不符")
        key = (p.task_id, p.annotator_id)
        if key in actors:
            raise ValueError("同一标注者/任务不能有两次不同随机展示")
        actors.add(key)
    histories = defaultdict(list)
    requests = set()
    for event in dataset.events:
        p, f = presentations.get(event.presentation_id), tasks.get(event.task_id)
        if not p or not f or p.task_id != event.task_id or p.annotator_id != event.source.labeler_id:
            raise ValueError("标签来源/展示身份与任务不符")
        if event.created_at < p.created_at:
            raise ValueError("标签时间早于展示")
        if (event.split, event.group_id, event.manifest_id) != (f.split, f.group_id, f.manifest_id):
            raise ValueError("标签的冻结分组不符")
        winner = p.left_id if event.decision == "left" else p.right_id if event.decision == "right" else None
        if event.winner_id != winner:
            raise ValueError("winner_id 与实际左右判定不符")
        key = (event.source.kind, event.source.labeler_id, event.request_id)
        if key in requests:
            raise ValueError("重复请求产生了多条事件")
        requests.add(key)
        histories[(event.source.kind, event.source.labeler_id, event.task_id)].append(event)
    for history in histories.values():
        history.sort(key=lambda e: e.revision)
        if [e.revision for e in history] != list(range(1, len(history) + 1)):
            raise ValueError("修订历史不连续，不能回放")
        if any(a.created_at > b.created_at for a, b in zip(history, history[1:], strict=False)):
            raise ValueError("修订时间倒序")
    return dataset


def read_jsonl(path: Path) -> list[Record]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(Record.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"JSONL 第 {number} 行不合法：{exc}") from exc
    validate_records(rows)
    return rows


class Store:
    def __init__(self, path: Path):
        self.path = path.resolve()

    @classmethod
    def initialize(cls, path: Path, manifest: Manifest) -> Store:
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 不覆盖其他数据库；标注内容默认仅当前系统用户可读。
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE records (kind TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY (kind, id))")
            conn.execute("PRAGMA user_version = 1")
            cls._insert(conn, [record("manifest", manifest)])
        return cls(path)

    @contextmanager
    def _connection(self, *, write=False):
        # mode=rw 防止拼错路径时悄悄新建空库；导入/建任务/标注才进入写事务。
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("不是受支持的独立标注库；请用 init 新建")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _records(conn) -> list[Record]:
        return [
            Record.model_validate_json(row[0]) for row in conn.execute("SELECT payload FROM records ORDER BY kind, id")
        ]

    @staticmethod
    def _insert(conn, rows: list[Record]):
        for item in rows:
            conn.execute(
                "INSERT OR IGNORE INTO records VALUES (?, ?, ?)",
                (item.record_type, record_id(item), canonical(item.model_dump(mode="json"))),
            )

    def snapshot(self) -> Dataset:
        with self._connection() as conn:
            return validate_records(self._records(conn))

    def import_records(self, rows: list[Record]) -> None:
        with self._connection(write=True) as conn:
            validate_records(self._records(conn) + rows)
            self._insert(conn, rows)

    def add_tasks(self, tasks: list[Task]) -> list[FrozenTask]:
        with self._connection(write=True) as conn:
            dataset = validate_records(self._records(conn))
            frozen = freeze(dataset.tasks, tasks, dataset.manifest)
            self._insert(conn, [record("task", f) for f in frozen])
            return frozen

    def present(self, task_id: str, annotator_id: str) -> Presentation:
        with self._connection(write=True) as conn:
            dataset = validate_records(self._records(conn))
            for p in dataset.presentations:
                if p.task_id == task_id and p.annotator_id == annotator_id:
                    return p
            task = next(f.task for f in dataset.tasks if f.task.task_id == task_id)
            left, right = task.candidates
            if secrets.randbits(1):
                left, right = right, left
            p = Presentation(
                presentation_id=str(uuid.uuid4()),
                task_id=task_id,
                annotator_id=annotator_id,
                left_id=left.candidate_id,
                right_id=right.candidate_id,
                created_at=datetime.now(UTC),
            )
            self._insert(conn, [record("presentation", p)])
            return p

    def label(self, request: LabelRequest, source: LabelSource) -> LabelEvent:
        with self._connection(write=True) as conn:
            dataset = validate_records(self._records(conn))
            for old in dataset.events:
                if (old.source.kind, old.source.labeler_id, old.request_id) == (
                    source.kind,
                    source.labeler_id,
                    request.request_id,
                ):
                    if (old.presentation_id, old.decision, old.revision, old.source) != (
                        request.presentation_id,
                        request.decision,
                        request.expected_revision + 1,
                        source,
                    ):
                        raise Conflict("幂等请求 ID 已用于另一操作")
                    return old
            p = next((p for p in dataset.presentations if p.presentation_id == request.presentation_id), None)
            if p is None or p.annotator_id != source.labeler_id:
                raise LookupError("没有属于当前标注者的展示")
            f = next(f for f in dataset.tasks if f.task.task_id == p.task_id)
            old = dataset.current(p.task_id, source)
            revision = old.revision if old else 0
            if request.expected_revision != revision:
                raise Conflict("标签版本已变化，请刷新后核对")
            if request.decision == "withdraw" and (old is None or old.decision == "withdraw"):
                raise Conflict("没有可撤销的标签")
            event = LabelEvent(
                event_id=str(uuid.uuid4()),
                request_id=request.request_id,
                task_id=p.task_id,
                presentation_id=p.presentation_id,
                source=source,
                decision=request.decision,
                winner_id=p.left_id
                if request.decision == "left"
                else p.right_id
                if request.decision == "right"
                else None,
                revision=revision + 1,
                created_at=datetime.now(UTC),
                split=f.split,
                group_id=f.group_id,
                manifest_id=f.manifest_id,
            )
            self._insert(conn, [record("label_event", event)])
            return event

    def export(self, source: Literal["human", "ai_weak", "all"] = "human") -> str:
        with self._connection() as conn:
            rows = self._records(conn)
        rows = [
            row
            for row in rows
            if row.record_type != "label_event" or source == "all" or row.data["source"]["kind"] == source
        ]
        validate_records(rows)
        return "".join(canonical(row.model_dump(mode="json")) + "\n" for row in rows)

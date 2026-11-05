"""独立 SQLite；对照结果不变，评价追加修订；付费生成先占位防止重复调用。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from researchlib.experiments.schema import RatingRequest, canonical, now


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path, snapshot_id: str):
        self.path = path.resolve()
        self.snapshot_id = snapshot_id
        if not path.is_file():
            raise ValueError("实验库不存在；先用 init 创建独立实验库")
        with self.connect() as conn:
            try:
                row = conn.execute("SELECT value FROM metadata WHERE key='snapshot'").fetchone()
                version = conn.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                if not row or row[0] != snapshot_id or not version or version[0] != "1":
                    raise ValueError("实验库版本或快照不匹配，请使用另一个实验库")
            except sqlite3.DatabaseError as exc:
                raise ValueError("不是已初始化的算法实验库") from exc

    @staticmethod
    def initialize(path: Path, snapshot_id: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        conn = sqlite3.connect(path)
        try:
            conn.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE ratings(
                    run_id TEXT, case_id TEXT, target TEXT, revision INTEGER,
                    request_id TEXT UNIQUE, payload TEXT NOT NULL,
                    PRIMARY KEY(run_id, case_id, target, revision));
                CREATE TABLE generations(
                    run_id TEXT, case_id TEXT, request_id TEXT UNIQUE, payload TEXT NOT NULL,
                    PRIMARY KEY(run_id, case_id));
            """)
            conn.executemany("INSERT INTO metadata VALUES (?,?)", [("snapshot", snapshot_id), ("schema", "1")])
            conn.commit()
        finally:
            conn.close()
        return Store(path, snapshot_id)

    @contextmanager
    def connect(self):
        # mode=rw：运行期丢失文件时不创建空库，也不跟随另一个不相关的文件名。
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save(self, payload):
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO runs VALUES (?,?)", (payload["id"], canonical(payload)))
        return self.get(payload["id"])

    def get(self, run_id):
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise LookupError("对照记录不存在")
        return json.loads(row[0])

    def history(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,payload FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
        return [{"id": row[0], "created_at": json.loads(row[1])["created_at"]} for row in rows]

    def ratings(self, run_id):
        with self.connect() as conn:
            rows = conn.execute("SELECT payload FROM ratings WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def generation(self, run_id, case_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM generations WHERE run_id=? AND case_id=?", (run_id, case_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def label(self, run_id, request: RatingRequest, *, actor="local-operator"):
        run = self.get(run_id)
        case = next((c for c in run["cases"] if c["id"] == request.case_id), None)
        if case is None:
            raise ValueError("问题不属于这次对照")
        if request.target == "answer":
            generation = self.generation(run_id, request.case_id)
            if not generation or generation["status"] != "done":
                raise ValueError("两边答案均完成后才能评价回答")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT payload FROM ratings WHERE request_id=?", (request.request_id,)).fetchone()
            if previous:
                event = json.loads(previous[0])
                if (event["run_id"] != run_id or event["request"] != request.model_dump()
                        or event.get("actor", "local-operator") != actor):
                    raise Conflict("请求编号已用于另一次评价")
                return event
            revision = conn.execute(
                "SELECT coalesce(max(revision),0) FROM ratings WHERE run_id=? AND case_id=? AND target=?",
                (run_id, request.case_id, request.target),
            ).fetchone()[0]
            if request.expected_revision != revision:
                raise Conflict("评价已更新，请刷新后修改")
            event = {"run_id": run_id, "request": request.model_dump(), "revision": revision + 1,
                     "created_at": now(), "actor": actor}
            conn.execute("INSERT INTO ratings VALUES (?,?,?,?,?,?)", (
                run_id, request.case_id, request.target, revision + 1, request.request_id, canonical(event),
            ))
            return event

    def daily_generation_count(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT count(*) FROM generations WHERE json_extract(payload,'$.created_at') LIKE ?",
                (now()[:10] + "%",),
            ).fetchone()[0]

    def claim_generation(self, run_id, case_id, request_id, provider, *, daily_limit=None, actor="local-operator"):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM generations WHERE run_id=? AND case_id=?", (run_id, case_id),
            ).fetchone()
            if row:
                return json.loads(row[0]), False
            if conn.execute("SELECT 1 FROM generations WHERE request_id=?", (request_id,)).fetchone():
                raise Conflict("请求编号已用于其他生成")
            if daily_limit is not None:
                count = conn.execute(
                    "SELECT count(*) FROM generations WHERE json_extract(payload,'$.created_at') LIKE ?",
                    (now()[:10] + "%",),
                ).fetchone()[0]
                if count >= daily_limit:
                    raise Conflict("今日生成组数额度已用尽；失败和未知结果也计入，不会调用模型")
            value = {"status": "running", "provider": provider, "created_at": now(), "sides": {},
                     "attempted_sides": [], "actor": actor}
            conn.execute("INSERT INTO generations VALUES (?,?,?,?)", (run_id, case_id, request_id, canonical(value)))
            return value, True

    def save_generation(self, run_id, case_id, value):
        with self.connect() as conn:
            conn.execute("UPDATE generations SET payload=? WHERE run_id=? AND case_id=?", (
                canonical(value), run_id, case_id,
            ))

    def export(self, run_id):
        run = self.get(run_id)
        generations = {c["id"]: self.generation(run_id, c["id"]) for c in run["cases"]}
        return {"run": run, "rating_events": self.ratings(run_id), "generations": generations,
                "summary": self.summary(run_id)}

    def summary(self, run_id):
        latest = {}
        for e in self.ratings(run_id):
            r = e["request"]
            latest[r["case_id"], r["target"]] = r["decision"]
        result = {}
        for target in ("search", "retrieval", "answer"):
            counts = {k: 0 for k in ("baseline", "candidate", "tie", "neither", "skip", "uncertain", "withdraw")}
            for (_, kind), decision in latest.items():
                if kind == target:
                    counts[decision] += 1
            directional = counts["baseline"] + counts["candidate"]
            result[target] = {"counts": counts, "directional_n": directional,
                              "candidate_preference": counts["candidate"] / directional if directional else None}
        return result

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from researchlib.ingest import projection_guard as guard
from researchlib.ingest import store
from researchlib.ingest.model import ParsedDoc
from researchlib.sources import Source
from researchlib.textutil import content_hash


def protected_row():
    body = "已验证回复的限定条件：尚未确认。"
    when = datetime(2026, 10, 20, tzinfo=UTC)
    identity = {
        "version": 1, "kind": "owner_reply_context", "source_id": "isolated-notes",
        "group_id": "isolated-group", "owner_account_id": "isolated-owner", "topic_id": "test-topic",
        "projection_md": body, "comments_archive": {"status": "complete", "sha256": "test-archive"},
    }
    row = {"id": 501, "title": "验证回复", "body_md": body, "body_text": body,
           "published_at": when, "content_hash": content_hash("验证回复", body, when),
           "raw_meta": {"poster_role": "owner_reply_context", "zsxq_identity": identity}}
    row["raw_meta"][guard.GUARD_KEY] = {
        "version": 1, "article_id": 501, "source_id": "isolated-notes", "external_id": "test-topic",
        "group_id": "isolated-group", "owner_account_id": "isolated-owner",
        "identity_sha256": guard.fingerprint(identity), "content_hash": row["content_hash"],
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    return row


def document(row, *, text=None, meta=None, newer=False):
    return ParsedDoc("test-topic", row["title"], row["published_at"] + timedelta(days=int(newer)),
                     row["body_md"] if text is None else text,
                     copy.deepcopy(row["raw_meta"] if meta is None else meta), [])


class Connection:
    def __init__(self, row, update_count=1):
        self.row, self.update_count = copy.deepcopy(row), update_count
        self.calls, self.held, self.revisions, self.events = [], {}, [], []
        self.one, self.rowcount = None, 1

    def execute(self, statement, params=None):
        sql = " ".join(statement.split())
        self.calls.append(sql)
        self.one, self.rowcount = None, 1
        if sql.startswith("SELECT id, content_hash"):
            self.one = copy.deepcopy(self.row)
        elif sql.startswith("INSERT INTO article_revisions"):
            self.revisions.append(params)
            if params[1] == "held":
                self.held[params[3]] = params
        elif sql.startswith("INSERT INTO articles"):
            self.one = {"id": 501}
        elif sql.startswith("UPDATE articles"):
            self.rowcount = self.update_count
        elif sql.startswith("SELECT count(*)"):
            self.one = {"n": 0}
        return self

    def fetchone(self):
        return self.one

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")
        self.revisions.clear()


@pytest.fixture
def isolated_store(monkeypatch):
    monkeypatch.setattr(store, "profile_for", lambda *a: store.EMPTY)
    monkeypatch.setattr(store, "sync_article_images", lambda *a, **k: (0, 0, 0))
    monkeypatch.setattr(store, "resolve_article", lambda *a: None)
    monkeypatch.setattr(store, "image_hashes", lambda *a: {})
    monkeypatch.setattr(store, "replace_chunks", lambda *a: None)
    monkeypatch.setattr(store, "replace_ocr_chunks", lambda *a: None)
    monkeypatch.setattr(store, "replace_mentions", lambda *a: None)
    monkeypatch.setattr(store, "load_company_rows", lambda *a: [])
    monkeypatch.setattr(store, "record_import", lambda *a: None)
    return Source("isolated-notes", "隔离测试", "隔离作者", "markdown", True)


def run(conn, source, doc):
    return store._store_one(conn, source, doc, Path("/unused-images"), store.CompanyMatcher([]))


def test_bound_marker_does_not_change_unmarked_rows():
    row = protected_row()
    assert guard.guard_state(row, "isolated-notes", "test-topic") == "verified"
    del row["raw_meta"][guard.GUARD_KEY]
    assert guard.guard_state(row, "isolated-notes", "test-topic") == "unmarked"


@pytest.mark.parametrize("field,value", [
    ("source_id", "other-source"), ("external_id", "other-topic"), ("article_id", 502),
    ("owner_account_id", "other-account"), ("identity_sha256", "bad"), ("content_hash", "bad"),
    ("body_sha256", "bad"), ("version", 9),
])
def test_binding_drift_cannot_approve_or_replace_a_projection(field, value):
    row = protected_row()
    row["raw_meta"][guard.GUARD_KEY][field] = value
    assert guard.guard_state(row, "isolated-notes", "test-topic") == "drifted"


def test_repeated_old_archive_is_retained_once_and_current_unchanged(isolated_store):
    row = protected_row()
    conn = Connection(row)
    old = document(row, text="旧归档只有网友提问。", meta={"poster_role": "guest", "poster_name": "测试网友"})
    for _ in range(2):
        assert run(conn, isolated_store, old)[0] == "held"
    assert len(conn.held) == 1 and conn.row == row
    held = next(iter(conn.held.values()))
    assert held[6] == old.body_md
    assert held[8].obj["_projection_conflict_input"]["incoming_raw_meta"] == old.raw_meta
    assert not any(q.startswith("UPDATE articles") or q.startswith("DELETE") for q in conn.calls)
    assert "FOR UPDATE" in conn.calls[0]


def test_newer_legitimate_edit_is_visible_and_recoverable_not_silently_discarded(isolated_store):
    from researchlib.revisions import label

    row = protected_row()
    doc = document(row, text="后来作者明确更正：条件尚未满足。", newer=True)
    conn = Connection(row)
    assert run(conn, isolated_store, doc)[0] == "held"
    held = next(iter(conn.held.values()))
    assert held[4:7] == (doc.title, doc.published_at, doc.body_md)
    assert label({"held": held[2]}) == "回复投影冲突待核"
    assert conn.row == row


def test_equal_body_different_metadata_keeps_distinct_inputs(isolated_store):
    row = protected_row()
    conn = Connection(row)
    for role in ["first-archive", "second-archive"]:
        assert run(conn, isolated_store, document(row, meta={"poster_role": role}))[0] == "held"
    assert len(conn.held) == 2


def test_identical_verified_input_does_not_reset_role_or_duplicate_revisions(isolated_store):
    row = protected_row()
    conn = Connection(row)
    assert run(conn, isolated_store, document(row))[0] == "unchanged"
    assert not conn.held and conn.row == row


def test_later_manual_edit_is_preserved_when_marker_is_stale(isolated_store):
    old = protected_row()
    current = copy.deepcopy(old)
    current["body_md"] = "后来合法人工编辑，仍要保留。"
    current["content_hash"] = content_hash(current["title"], current["body_md"], current["published_at"])
    conn = Connection(current)
    assert run(conn, isolated_store, document(old))[0] == "held"
    assert conn.row == current


def test_incoming_marker_change_is_a_retained_metadata_conflict(isolated_store):
    row = protected_row()
    incoming = document(row)
    incoming.raw_meta[guard.GUARD_KEY]["owner_account_id"] = "other-account"
    conn = Connection(row)
    assert run(conn, isolated_store, incoming)[0] == "held"
    assert len(conn.held) == 1 and conn.row == row


def test_normalized_body_hash_does_not_discard_distinct_original_text(isolated_store):
    row = protected_row()
    conn = Connection(row)
    for text in ["保留原始换行\r\n与完整输入。", "保留原始换行\n与完整输入。"]:
        assert run(conn, isolated_store, document(row, text=text, meta={}))[0] == "held"
    assert len(conn.held) == 2


@pytest.mark.parametrize("insert", [False, True])
def test_non_target_normal_insert_and_update_remain_available(isolated_store, insert):
    row = protected_row()
    del row["raw_meta"][guard.GUARD_KEY]
    row["raw_meta"] = {}
    conn = Connection(None if insert else row)
    doc = document(row, text="正常文章的新版本，保留普通导入。", meta={})
    assert run(conn, isolated_store, doc)[0] == ("inserted" if insert else "updated")
    assert not conn.held


@pytest.mark.parametrize("metadata_only", [False, True])
def test_stale_writer_aborts_and_wrapper_retains_input(isolated_store, metadata_only):
    row = protected_row()
    del row["raw_meta"][guard.GUARD_KEY]
    row["raw_meta"] = {}
    conn = Connection(row, update_count=0)
    doc = document(row, text=None if metadata_only else "旧进程的正文改动，不可覆盖并发修复。",
                   meta={"poster_name": "测试来稿"})
    report = store.store_documents(conn, source=isolated_store, docs=[doc], data_dir=Path("/unused"),
                                   archive_files=1, images_in_archive=0, errors=[])
    assert report.held == 1 and report.errors and len(conn.held) == 1
    assert conn.events[:2] == ["rollback", "commit"]
    assert next(iter(conn.held.values()))[2] == "projection_concurrent"

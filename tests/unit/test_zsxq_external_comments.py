"""External archive regressions use synthetic text and the existing trusted binding."""
import json
from pathlib import Path

import pytest

from researchlib.eligibility import IDENTITY_KEY, TRUSTED_OWNERS, comment_evidence, is_eligible_article
from researchlib.ingest.parse import load_raw_index, parse_archive

SOURCE = "fangwei-zsxq"
GROUP, OWNER = TRUSTED_OWNERS[SOURCE]


def archive(root: Path, *, preview=False, overrides=None):
    reply = {"comment_id": "200", "owner": {"user_id": OWNER}, "text": "Verified research reply.",
             "replied_comments": [{"comment_id": "201", "owner": {"user_id": "guest-fixture"},
                                   "parent_comment_id": "200", "text": "[smile]"}]}
    topic = {"topic_id": "424242", "group": {"group_id": GROUP}, "type": "talk",
             "create_time": "2026-01-01T10:00:00+0800", "comments_count": 2,
             "talk": {"owner": {"user_id": "guest-fixture"}, "text": "Guest question."},
             "show_comments": [reply] if preview else []}
    payload = {"topic_id": "424242", "comments_count_at_fetch": 2, "fetched_count": 2,
               "top_level_count": 1, "comments": [reply], "incomplete_reply_threads": []}
    payload.update(overrides or {})
    for directory in ("md", "raw/topics", "raw/comments"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "md/424242.md").write_text("# Guest question\n\nGuest question.\n")
    topic_path, comment_path = root / "raw/topics/424242.json", root / "raw/comments/424242.json"
    topic_path.write_text(json.dumps(topic))
    comment_path.write_text(json.dumps(payload))
    return topic_path, comment_path


@pytest.mark.parametrize("preview", [False, True])
def test_complete_archive_restores_owner_reply_without_guest_emoji_or_guessed_question(tmp_path, preview):
    root = tmp_path / "archive"
    archive(root, preview=preview)
    docs, errors, _, _ = parse_archive(root, tmp_path / "work", id_mode="zsxq", source_id=SOURCE)
    assert errors == [] and len(docs) == 1
    doc = docs[0]
    assert is_eligible_article({"source_id": SOURCE, "raw_meta": doc.raw_meta})
    assert "Verified research reply." in doc.body_md and "[smile]" not in doc.body_md
    assert "Guest question." not in doc.body_md
    proof = doc.raw_meta[IDENTITY_KEY]
    assert proof["replies"][0]["question"] == ""
    assert proof["comments_archive"]["status"] == "complete"
    assert proof["comments_archive"]["observed_unique_count"] == 2


@pytest.mark.parametrize("overrides,reason", [
    ({"topic_id": "wrong-topic"}, "topic_id_mismatch_or_missing"),
    ({"source_id": "other-source"}, "source_id_mismatch"),
    ({"group_id": "other-group"}, "group_id_mismatch"),
    ({"comments": [{"comment_id": "1", "topic_id": "wrong-topic"}]}, "comment_binding_mismatch"),
])
def test_wrong_binding_never_admits_external_reply(tmp_path, overrides, reason):
    paths = archive(tmp_path / "archive", overrides=overrides)
    meta = load_raw_index(list(paths), source_id=SOURCE)["424242"]
    proof = meta[IDENTITY_KEY]
    assert proof["kind"] == "excluded" and proof["replies"] == []
    assert proof["comments_archive"]["status"] == "rejected"
    assert proof["comments_archive"]["reason"] == reason


def test_same_filename_in_another_raw_root_does_not_bind(tmp_path):
    topic, _ = archive(tmp_path / "one")
    _, other_comments = archive(tmp_path / "two")
    meta = load_raw_index([topic, other_comments], source_id=SOURCE)["424242"]
    assert meta[IDENTITY_KEY]["kind"] == "excluded"
    assert "comments_archive" not in meta[IDENTITY_KEY]


def test_symlinked_comment_directory_cannot_cross_raw_roots(tmp_path):
    topic, comments = archive(tmp_path / "one")
    _, other_comments = archive(tmp_path / "two")
    comments.unlink()
    comments.parent.rmdir()
    comments.parent.symlink_to(other_comments.parent, target_is_directory=True)
    proof = load_raw_index([topic, comments], source_id=SOURCE)["424242"][IDENTITY_KEY]
    assert proof["kind"] == "excluded" and "comments_archive" not in proof


def test_partial_archive_preserves_preview_and_reports_incompleteness(tmp_path):
    paths = archive(tmp_path / "archive", preview=True, overrides={
        "comments": [], "fetched_count": 0, "incomplete_reply_threads": ["thread-fixture"],
    })
    proof = load_raw_index(list(paths), source_id=SOURCE)["424242"][IDENTITY_KEY]
    assert proof["kind"] == "owner_reply_context"
    assert proof["comments_archive"]["status"] == "partial"
    assert proof["comments_archive"]["incomplete_reply_threads"] == ["thread-fixture"]


@pytest.mark.parametrize("top_level", [2, -1, True, "1"])
def test_inconsistent_top_level_count_keeps_independently_verified_preview(tmp_path, top_level):
    guest = {"comment_id": "400", "owner": {"user_id": "guest-fixture"}, "text": "Other text.",
             "replied_comments": [{"comment_id": "401", "text": "Other reply."}]}
    paths = archive(tmp_path / "archive", preview=True, overrides={
        "comments": [guest], "top_level_count": top_level,
    })
    proof = load_raw_index(list(paths), source_id=SOURCE)["424242"][IDENTITY_KEY]
    assert proof["kind"] == "owner_reply_context"
    assert proof["comments_archive"]["status"] == "partial"
    assert proof["comments_archive"]["observed_top_level_count"] == 1
    assert proof["replies"][0]["text"] == "Verified research reply."


def test_different_duplicate_archive_candidates_are_rejected(tmp_path, monkeypatch):
    topic, comments = archive(tmp_path / "archive")
    candidates = iter([comments.read_bytes(), b'{"topic_id":"424242","comments":[]}'])
    original_read = Path.read_bytes
    # Also works on case-insensitive filesystems: the candidate changes between reads.
    monkeypatch.setattr(Path, "read_bytes", lambda path: next(candidates) if path == comments else original_read(path))
    proof = load_raw_index([topic, comments, comments], source_id=SOURCE)["424242"][IDENTITY_KEY]
    assert proof["kind"] == "excluded"
    assert proof["comments_archive"]["reason"] == "ambiguous_archive_candidates"


def test_repeated_parent_keeps_later_children_and_conflicting_ids_are_quarantined():
    parent = {"comment_id": "p", "owner": {"user_id": "guest-fixture"}, "text": "Question."}
    child = {"comment_id": "c", "owner": {"user_id": OWNER}, "text": "Verified answer."}
    rows, conflicts = comment_evidence({"comments": [parent, {**parent, "replied_comments": [child]}]})
    assert conflicts == [] and [row["comment_id"] for row in rows] == ["p", "c"]
    assert rows[1]["parent_comment_id"] == "p"
    rows, conflicts = comment_evidence({"comments": [child, {**child, "owner": {"user_id": "guest-fixture"}}]})
    assert rows == [] and conflicts == ["c"]


def test_conflicting_parent_and_parent_cycle_cannot_supply_question_context():
    child = {"comment_id": "c", "parent_comment_id": "other", "owner": {"user_id": OWNER}, "text": "Answer."}
    rows, conflicts = comment_evidence({"comments": [{"comment_id": "p", "replied_comments": [child]}]})
    assert "c" in conflicts and all(row["comment_id"] != "c" for row in rows)
    rows, conflicts = comment_evidence({"comments": [
        {"comment_id": "a", "parent_comment_id": "b"}, {"comment_id": "b", "parent_comment_id": "a"},
    ]})
    assert rows == [] and conflicts == ["a", "b"]

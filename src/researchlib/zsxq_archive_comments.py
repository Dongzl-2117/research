"""Bind external comment archives to their exact verified topic and raw root."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from researchlib.eligibility import TRUSTED_OWNERS, comment_evidence, topic_group_id, topic_poster


def raw_scope(path: Path) -> Path | None:
    return next((parent.resolve() for parent in path.parents if parent.name == "raw"), None)


def load_comment_archives(paths: list[Path]) -> dict[tuple[Path, str], dict]:
    archives = {}
    for path in paths:
        if path.parent.name != "comments" or path.parent.parent.name != "raw" or path.is_symlink():
            continue
        scope = raw_scope(path)
        if scope is None or path.resolve().parent != scope / "comments":
            continue
        record = {"path": f"raw/comments/{path.name}", "status": "rejected"}
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            record["reason"] = "unreadable_or_invalid_json"
        else:
            record.update(payload=payload, sha256=hashlib.sha256(raw).hexdigest())
        key = (scope, path.stem)
        previous = archives.get(key)
        if previous is not None and (previous.get("reason") == "ambiguous_archive_candidates"
                                     or previous.get("sha256") != record.get("sha256")):
            record = {"path": record["path"], "status": "rejected",
                      "reason": "ambiguous_archive_candidates"}
        archives[key] = record
    return archives


def bind_comment_archive(node: dict, path: Path, source_id: str, archives: dict) -> None:
    topic_id = str(node.get("topic_id", node.get("topicId", "")))
    group_id = topic_group_id(node)
    source = source_id or next((sid for sid, (gid, _) in TRUSTED_OWNERS.items() if gid == group_id), "")
    if (source not in TRUSTED_OWNERS or group_id != TRUSTED_OWNERS[source][0]
            or node.get("source_id") not in (None, "", source) or not topic_poster(node)):
        return
    scope = raw_scope(path)
    if scope is None or not path.resolve().is_relative_to(scope):
        return
    record = archives.get((scope, topic_id))
    if record is None:
        return
    proof = {key: record[key] for key in ("path", "sha256", "reason") if key in record}
    proof.update(source_id=source, group_id=group_id, topic_id=topic_id, status="rejected")
    node["comments_archive"] = proof
    if "payload" not in record:
        return
    payload = record.get("payload")
    if not isinstance(payload, dict) or str(payload.get("topic_id", "")) != topic_id:
        proof["reason"] = "topic_id_mismatch_or_missing"
        return
    if payload.get("source_id") not in (None, "", source):
        proof["reason"] = "source_id_mismatch"
        return
    declared_group = topic_group_id(payload)
    if declared_group and declared_group != group_id:
        proof["reason"] = "group_id_mismatch"
        return
    comments = payload.get("comments")
    if not isinstance(comments, list):
        proof["reason"] = "comments_not_array"
        return

    def consistent(items, depth=0):
        if depth > 30:
            return False
        for item in items:
            if not isinstance(item, dict):
                return False
            if item.get("topic_id") is not None and str(item["topic_id"]) != topic_id:
                return False
            if item.get("source_id") not in (None, "", source):
                return False
            gid = topic_group_id(item)
            if gid and gid != group_id:
                return False
            children = item.get("replied_comments") or []
            if not isinstance(children, list) or not consistent(children, depth + 1):
                return False
        return True

    if not consistent(comments):
        proof["reason"] = "comment_binding_mismatch"
        return
    flat, conflicts = comment_evidence({"comments": comments})
    observed = len({str(c["comment_id"]) for c in flat if c.get("comment_id") is not None})
    expected = payload.get("comments_count_at_fetch")
    fetched = payload.get("fetched_count")
    incomplete = payload.get("incomplete_reply_threads")
    declared = node.get("comments_count")
    top_level = payload.get("top_level_count")
    observed_top_level = sum(not c.get("parent_comment_id") for c in flat)
    top_level_valid = (top_level is None or
                       (type(top_level) is int and top_level >= 0 and top_level == observed_top_level))
    counts_valid = type(expected) is int and type(fetched) is int and expected >= 0 and fetched >= 0
    complete = (
        counts_valid and fetched == observed and observed >= expected and incomplete == []
        and not conflicts and top_level_valid and (type(declared) is not int or observed >= declared)
    )
    proof.update(
        status="complete" if complete else "partial", comments_count_at_fetch=expected,
        fetched_count=fetched, observed_unique_count=observed,
        top_level_count=top_level, observed_top_level_count=observed_top_level,
        fetched_at=payload.get("fetched_at"),
        incomplete_reply_threads=copy.deepcopy(incomplete), comment_conflict_ids=conflicts,
        binding="same_raw_root_and_verified_topic_source_group",
    )
    # A partial archive must not erase an independently observed valid preview.
    preview = node.get("comments") if isinstance(node.get("comments"), list) else node.get("show_comments")
    node["comments"] = copy.deepcopy(comments + (preview or []) if not complete else comments)

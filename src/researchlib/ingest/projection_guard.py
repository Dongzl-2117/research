"""Preserve explicitly repaired reply projections; this never grants eligibility.

Only an independently verified targeted repair installs the bound marker. A new
edit is retained as a held version for review, not automatically trusted or lost.
"""

from __future__ import annotations

import hashlib
import json

from researchlib.revisions import save_revision
from researchlib.textutil import content_hash

GUARD_KEY = "_verified_reply_projection"


def fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def guard_state(row, source_id: str, external_id: str) -> str:
    meta = row.get("raw_meta") or {}
    if GUARD_KEY not in meta:
        return "unmarked"
    guard = meta[GUARD_KEY]
    identity = meta.get("zsxq_identity") or {}
    if not isinstance(guard, dict) or not isinstance(identity, dict):
        return "drifted"
    expected = {
        "version": 1, "article_id": row["id"], "source_id": source_id, "external_id": external_id,
        "group_id": identity.get("group_id"), "owner_account_id": identity.get("owner_account_id"),
        "identity_sha256": fingerprint(identity), "content_hash": row["content_hash"],
        "body_sha256": hashlib.sha256(row["body_md"].encode()).hexdigest(),
    }
    valid = (
        guard == expected and type(guard.get("version")) is int and type(identity.get("version")) is int
        and identity.get("version") == 1
        and identity.get("kind") == "owner_reply_context" and meta.get("poster_role") == "owner_reply_context"
        and identity.get("source_id") == source_id and str(identity.get("topic_id")) == external_id
        and bool(identity.get("group_id")) and bool(identity.get("owner_account_id"))
        and identity.get("projection_md") == row["body_md"]
        and isinstance(identity.get("comments_archive"), dict)
        and identity["comments_archive"].get("status") == "complete"
        and content_hash(row["title"], row["body_md"], row["published_at"]) == row["content_hash"]
    )
    return "verified" if valid else "drifted"


def identical_input(row, doc, digest: str) -> bool:
    # Do not let the legacy role assignment rewrite owner_reply_context to guest.
    meta = {k: v for k, v in (row["raw_meta"] or {}).items() if k != GUARD_KEY}
    incoming = {k: v for k, v in doc.raw_meta.items() if k != GUARD_KEY}
    supplied_guard = doc.raw_meta.get(GUARD_KEY)
    return (
        digest == row["content_hash"] and incoming == meta and not doc.image_refs
        and (GUARD_KEY not in doc.raw_meta or supplied_guard == row["raw_meta"][GUARD_KEY])
    )


def hold_input(conn, source_id, article_id, doc, digest, body_text, *, reason):
    payload = {
        "source_id": source_id, "external_id": doc.external_id, "incoming_content_hash": digest,
        "incoming_raw_meta": doc.raw_meta,
        "incoming_published_at": doc.published_at.isoformat() if doc.published_at is not None else None,
        "image_refs": [[ref, str(path) if path is not None else None] for ref, path in doc.image_refs],
    }
    # A body-only key would silently lose distinct metadata for the same body.
    # The explicitly typed key identifies the whole retained input, not its body.
    key = "guard:" + fingerprint({**payload, "title": doc.title, "body_md": doc.body_md})
    save_revision(conn, article_id, kind="held", reason=reason, content_hash=key,
                  title=doc.title, published_at=doc.published_at, body_md=doc.body_md,
                  body_text=body_text, raw_meta={"_projection_conflict_input": payload})


class ConcurrentProjectionChange(Exception):
    """A row became protected or changed after the old importer read it."""

    def __init__(self, article_id, digest, body_text):
        super().__init__("受保护文章发生并发变化")
        self.article_id, self.digest, self.body_text = article_id, digest, body_text

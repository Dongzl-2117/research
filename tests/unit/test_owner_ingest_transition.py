"""Policy projection shrink must not be mistaken for lost owner writing."""

from researchlib.boilerplate import EMPTY
from researchlib.eligibility import IDENTITY_KEY, identity_for_topic
from researchlib.ingest import store
from researchlib.ingest.model import ParsedDoc
from researchlib.revisions import split_comments
from researchlib.sources import Source

SOURCE = Source(id="fangwei-zsxq", name="test", author="方伟", kind="zsxq", enabled=True)
OWNER = "1825524112"


class Connection:
    def __init__(self, old):
        self.old = old
        self.statements = []
        self.result = None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "SELECT id, content_hash" in sql:
            self.result = self.old
        elif "SELECT count(*) AS n" in sql:
            self.result = {"n": 0}
        elif "SELECT title, body_md, raw_meta, published_at" in sql:
            self.result = None
        else:
            self.result = None
        return self

    def fetchone(self):
        return self.result


def setup(monkeypatch):
    snapshots = []
    monkeypatch.setattr(store, "profile_for", lambda *_: EMPTY)
    monkeypatch.setattr(store, "held_before", lambda *_: False)
    monkeypatch.setattr(store, "save_revision", lambda _conn, _id, **kw: snapshots.append(kw))
    monkeypatch.setattr(store, "mentions_for_document", lambda *_: [])
    monkeypatch.setattr(store, "replace_mentions", lambda *_: None)
    monkeypatch.setattr(store, "replace_chunks", lambda *_: None)
    monkeypatch.setattr(store, "replace_ocr_chunks", lambda *_: None)
    monkeypatch.setattr(store, "sync_article_images", lambda *_, **kw: (0, 0, 0))
    monkeypatch.setattr(store, "image_hashes", lambda *_: {})
    monkeypatch.setattr(store, "resolve_article", lambda *_: None)
    return snapshots


def old_row(body, poster="7"):
    return {"id": 17, "content_hash": "old", "title": "old title", "published_at": None,
            "body_md": body, "body_text": body, "raw_meta": {"poster_id": poster, "poster_role": "guest"}}


def new_doc(body, *, poster=OWNER, replies=None):
    topic = {"topic_id": "123456", "group": {"group_id": "48888181884848"},
             "talk": {"owner": {"user_id": poster}, "text": body}, "comments": replies or []}
    return ParsedDoc("123456", "new title", None, body,
                     {IDENTITY_KEY: identity_for_topic(topic, SOURCE.id), "poster_id": poster}, [])


def test_guest_to_verified_owner_reply_is_activated_and_original_preserved(monkeypatch, tmp_path):
    snapshots = setup(monkeypatch)
    original = "Guest proposition about fabrication capacity. " * 30
    conn = Connection(old_row(original))
    doc = new_doc("unselected guest body", poster="7", replies=[
        {"comment_id": "1", "owner": {"user_id": OWNER}, "text": "Owner verified answer only."},
    ])
    assert store._store_one(conn, SOURCE, doc, tmp_path, None)[0] == "updated"
    assert any("UPDATE articles SET" in sql for sql, _ in conn.statements)
    assert len(snapshots) == 1 and snapshots[0]["kind"] == "previous"
    assert snapshots[0]["body_md"] == original and snapshots[0]["raw_meta"]["poster_id"] == "7"
    assert "Guest proposition" not in doc.body_md and "Owner verified answer" in doc.body_md


def test_genuine_verified_owner_body_truncation_remains_held(monkeypatch, tmp_path):
    snapshots = setup(monkeypatch)
    original = "Owner's original investment reasoning. " * 30
    conn = Connection(old_row(original, poster=OWNER))
    doc = new_doc("A severely shortened owner text.")
    assert store._store_one(conn, SOURCE, doc, tmp_path, None)[0] == "held"
    assert not any("UPDATE articles SET" in sql for sql, _ in conn.statements)
    assert len(snapshots) == 1 and snapshots[0]["kind"] == "held" and snapshots[0]["reason"] == "shrunk"


def test_removing_unverified_legacy_comments_compares_owner_projection(monkeypatch, tmp_path):
    snapshots = setup(monkeypatch)
    owner_body = "Owner's actual paragraph."
    original = owner_body + "\n\n## 评论（1）\n\n" + "Unverified guest prose. " * 80
    previous = old_row(original, poster=OWNER)
    previous["title"] = "new title"
    conn = Connection(previous)
    doc = new_doc(owner_body)
    assert store._store_one(conn, SOURCE, doc, tmp_path, None)[0] == "updated"
    assert snapshots[0]["kind"] == "previous" and snapshots[0]["body_md"] == original
    assert not any(snapshot["kind"] == "held" for snapshot in snapshots)


def test_canonical_reply_boundary_uses_first_marker_and_ignores_fenced_examples():
    body = "owner body\n\n## 星主回复（账户已核实）\n\nfirst\n\n## 评论里的回复\n\nsecond"
    main, comments = split_comments(body)
    assert main == "owner body" and comments.startswith("\n\n## 星主回复（账户已核实）")
    fenced = "```md\n\n## 星主回复（账户已核实）\n\ncode example\n```"
    assert split_comments(fenced) == (fenced, "")

"""No embedding request may receive a stale or unowned cached chunk."""

from contextlib import contextmanager

import pytest

from researchlib import embeddings
from researchlib.eligibility import IDENTITY_KEY, chunk_provenance_article, identity_for_topic, project_article


def current_row(*, lang="", ordinal=0, body="current owner body"):
    topic = {"topic_id": "123456", "group": {"group_id": "48888181884848"},
             "talk": {"owner": {"user_id": "1825524112"}}}
    article = project_article({"source_id": "fangwei-zsxq", "title": "owner title",
                               "body_md": "current owner body", "raw_meta": {
                                   IDENTITY_KEY: identity_for_topic(topic, "fangwei-zsxq")}})
    article["raw_meta"][IDENTITY_KEY]["projection_md"] = article["body_md"]
    return {"article_id": 7, "ord": ordinal, "lang": lang, "heading": "", "body": body,
            "anchor": "", "title": "owner title", "source_id": "fangwei-zsxq",
            "article_body_md": article["body_md"], "article_body_text": article["body_text"],
            "raw_meta": article["raw_meta"], "content_hash": "current-hash", "hash": "chunk-hash"}


class Connection:
    def __init__(self, row, *, translation=None, images=()):
        self.row = row
        self.translation = translation
        self.images = list(images)
        self.current = None
        self.written = []

    def execute(self, sql, params=None):
        if "FROM unnest" in sql:
            self.current = [self.row]
        elif "FROM article_translations WHERE" in sql:
            self.current = self.translation
        elif "FROM article_images i JOIN image_texts" in sql:
            self.current = self.images
        else:
            self.current = []
        return self

    def fetchall(self):
        return self.current

    def fetchone(self):
        return self.current

    def commit(self):
        pass

    @contextmanager
    def cursor(self):
        yield self

    def executemany(self, sql, rows):
        self.written.extend(rows)


def setup_backfill(monkeypatch, row, *, forbidden=True):
    pending = [[{"article_id": 7, "ord": row["ord"]}], []]
    monkeypatch.setattr(embeddings, "column_type", lambda _conn: "vector")
    monkeypatch.setattr(embeddings, "model_name", lambda: "offline-stub")
    monkeypatch.setattr(embeddings, "_pending", lambda *_args: pending.pop(0))
    calls = []

    def embed(texts):
        if forbidden:
            raise AssertionError("unsafe text reached an embedding request")
        calls.extend(texts)
        return [[0.25, 0.75] for _ in texts]

    monkeypatch.setattr(embeddings, "embed_texts", embed)
    return calls


def test_backfill_rechecks_stale_guest_translation_before_external_request(monkeypatch):
    row = current_row(lang="zh", ordinal=10000, body="old guest-only translation")
    setup_backfill(monkeypatch, row)
    conn = Connection(row, translation={"content_hash": "old-hash", "complete": True,
                                       "title_zh": "", "body_text": "old guest-only translation"})
    assert embeddings.backfill(conn, limit=1) == 0
    assert conn.written == []


def test_backfill_rechecks_account_changed_since_pending_selection(monkeypatch):
    row = current_row()
    row["raw_meta"] = {"poster_id": "unverified", "poster_name": "方伟", "poster_role": "owner"}
    setup_backfill(monkeypatch, row)
    assert embeddings.backfill(Connection(row), limit=1) == 0


@pytest.mark.parametrize("flag", ["hidden", "suppressed"])
def test_backfill_rechecks_visibility_before_external_request(monkeypatch, flag):
    row = current_row()
    row["raw_meta"][flag] = "source_paused"
    setup_backfill(monkeypatch, row)
    conn = Connection(row)
    assert embeddings.backfill(conn, limit=1) == 0
    assert conn.written == []


def test_backfill_excludes_removed_guest_image_even_on_canonical_owner_article(monkeypatch):
    row = current_row(lang="ocr", ordinal=20000, body="old guest screenshot words")
    row.update(anchor="img-" + "a" * 12, heading="图片里的字")
    setup_backfill(monkeypatch, row)
    conn = Connection(row, images=[{"original_ref": "guest.png", "content_hash": "a" * 64,
                                   "text": "old guest screenshot words"}])
    assert embeddings.backfill(conn, limit=1) == 0


def test_backfill_embeds_a_freshly_proven_current_owner_chunk(monkeypatch):
    row = current_row()
    calls = setup_backfill(monkeypatch, row, forbidden=False)
    conn = Connection(row)
    assert embeddings.backfill(conn, limit=1) == 1
    assert calls == ["owner title\ncurrent owner body"]
    assert len(conn.written) == 1


def test_ocr_heading_itself_needs_current_generated_provenance():
    article = {"source_id": "fangwei-mp", "body_md": "![](owner.png)", "body_text": "",
               "content_hash": "current", "raw_meta": {"ocr_role_version": 3},
               "ocr_text": "未确认：safe owner words"}
    image = {"content_hash": "a" * 64, "original_ref": "owner.png", "text": "未确认：safe owner words"}
    chunk = {"ord": 20000, "lang": "ocr", "anchor": "img-" + "a" * 12,
             "heading": "stale guest heading", "body": "未确认：safe owner words"}
    assert not chunk_provenance_article(article, chunk, images=[image])
    chunk["heading"] = "图片里的字"
    assert chunk_provenance_article(article, chunk, images=[image])

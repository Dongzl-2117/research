"""No OCR/cache work for unverified or guest-owned image sets; no live DB."""

from copy import deepcopy

import pytest

from researchlib import ocr
from researchlib.eligibility import TRUSTED_OWNERS


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, articles):
        self.articles = articles
        self.calls = []
        self.commits = 0

    def execute(self, sql, params):
        query = " ".join(sql.split())
        self.calls.append((query, params))
        if query.startswith("SELECT a.id,") or query.startswith("SELECT DISTINCT a.id AS article_id"):
            return Rows(self.articles)
        if query.startswith("INSERT INTO image_texts"):
            return Rows([])
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def article(kind="owner_post", *, canonical=True):
    group_id, owner_id = TRUSTED_OWNERS["fangwei-zsxq"]
    body = "星主正文。\n\n![](owner.png)" if kind == "owner_post" else "星主回复：\n\n星主回复正文。"
    identity = {
        "version": 1,
        "source_id": "fangwei-zsxq",
        "group_id": group_id,
        "topic_id": "topic-1",
        "owner_account_id": owner_id,
        "poster_account_id": owner_id if kind == "owner_post" else "guest-1",
        "kind": kind,
        "projection_md": body if canonical else "old unrelated text",
        "replies": [{"comment_id": "reply-1", "author_account_id": owner_id, "text": "星主回复正文。"}],
    }
    return {
        "id": 1, "article_id": 1, "source_id": "fangwei-zsxq", "source_kind": "zsxq",
        "raw_meta": {"zsxq_identity": identity}, "body_md": body,
        "original_ref": "owner.png",
        "ocr_text": "网友的旧图文内容，不可进入星主观点。", "ocr_hash": "legacy",
    }


@pytest.mark.parametrize("row", [
    article("owner_reply_context"),
    article("excluded"),
    article(canonical=False),
    {"id": 1, "source_id": "fangwei-zsxq", "raw_meta": {"poster_role": "owner"}},
])
@pytest.mark.parametrize("function,expected", [(ocr.article_ocr, ("", "")), (ocr.refresh_article_ocr, "")])
def test_unsafe_article_never_reads_image_cache_or_writes_article(row, function, expected):
    conn = Connection([deepcopy(row)])
    assert function(conn, 1) == expected
    assert len(conn.calls) == 1
    assert conn.calls[0][0].startswith("SELECT a.id,")
    assert conn.commits == 0


def test_pending_image_query_excludes_reply_context_even_when_text_projection_is_safe():
    class Candidates:
        def execute(self, sql, params):
            query = " ".join(sql.split())
            assert "projection_md" in query
            assert "IS DISTINCT FROM 'owner_reply_context'" in query
            assert "a.duplicate_of IS NULL" in query
            assert "i.article_id = ANY(%s)" in query
            assert params == ([1], 5)
            return Rows([])

    assert ocr.pending_images(Candidates(), 5, redo=True, article_ids=[1]) == []


def test_executor_rechecks_stale_candidate_before_recognizing(monkeypatch, tmp_path):
    conn = Connection([article("owner_reply_context")])
    monkeypatch.setattr(ocr, "pending_images", lambda *args, **kwargs: [
        {"content_hash": "shared", "storage_path": "guest.png"},
    ])
    report = ocr.run_ocr(conn, tmp_path, 5, lambda path: pytest.fail("guest image reached OCR"))
    assert report["images"] == 0
    assert all(sql.startswith("SELECT") for sql, _ in conn.calls)
    assert conn.commits == 0


def test_leftover_guest_attachment_is_not_an_eligible_owner_image(monkeypatch, tmp_path):
    owner = article()
    owner["original_ref"] = "old-comment-image.png"
    conn = Connection([owner])
    monkeypatch.setattr(ocr, "pending_images", lambda *args, **kwargs: [
        {"content_hash": "stale-guest", "storage_path": "old-comment-image.png"},
    ])
    report = ocr.run_ocr(conn, tmp_path, 5, lambda path: pytest.fail("orphan guest attachment reached OCR"))
    assert report["images"] == 0
    assert conn.commits == 0


def test_shared_image_updates_only_eligible_selected_references(monkeypatch, tmp_path):
    owner = article()
    guest = article("owner_reply_context")
    guest["article_id"] = 2
    conn = Connection([owner, guest])
    root = tmp_path / "images"
    root.mkdir()
    (root / "shared.png").write_bytes(b"test image stub")
    monkeypatch.setattr(ocr, "pending_images", lambda *args, **kwargs: [
        {"content_hash": "shared", "storage_path": "shared.png"},
    ])
    chunks, refreshed = [], []
    monkeypatch.setattr(ocr, "replace_ocr_chunks", lambda conn, article_id: chunks.append(article_id) or True)
    monkeypatch.setattr(ocr, "refresh_article_ocr", lambda conn, article_id: refreshed.append(article_id))
    report = ocr.run_ocr(conn, tmp_path, 5, lambda path: "只识别通过身份核验的图片文字。", article_ids=[1])
    assert report["images"] == 1
    assert chunks == refreshed == [1]
    assert conn.calls[0][1] == ("shared", [1])


@pytest.mark.parametrize("storage_path", ["../../outside.png", "/tmp/outside.png"])
def test_executor_rejects_image_paths_outside_its_root(monkeypatch, tmp_path, storage_path):
    conn = Connection([article()])
    monkeypatch.setattr(ocr, "pending_images", lambda *args, **kwargs: [
        {"content_hash": "unsafe", "storage_path": storage_path},
    ])
    report = ocr.run_ocr(conn, tmp_path, 5, lambda path: pytest.fail("escaped image root"))
    assert report["images"] == 0 and report["missing"] == 1
    assert conn.commits == 0

"""Policy must hold at explicit executors, including direct article-id callers."""

from types import SimpleNamespace

from researchlib import chunks, digest, theses, views
from researchlib.eligibility import TRUSTED_OWNERS, project_article


class Connection:
    def __init__(self, row=None):
        self.row = row
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return self

    def fetchone(self):
        return self.row

    def commit(self):
        pass


def article(meta):
    return {
        "id": 7, "source_id": "fangwei-zsxq", "title": "Untrusted guest title",
        "body_md": "Guest advertising and unverified interpretation.",
        "body_text": "Guest advertising and unverified interpretation.",
        "published_at": None, "content_hash": "fixture", "raw_meta": meta,
    }


def test_explicit_guest_executors_do_not_call_model_or_write(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("excluded content reached processing")

    monkeypatch.setattr(digest, "chat", forbidden)
    monkeypatch.setattr(digest, "article_ocr", forbidden)
    monkeypatch.setattr(views, "chat", forbidden)
    monkeypatch.setattr(views, "article_ocr", forbidden)
    row = article({"poster_id": "unverified", "poster_name": "方伟", "poster_role": "owner"})
    conn = Connection()
    settings = SimpleNamespace(display_timezone="Asia/Shanghai")
    assert digest.digest_article(conn, settings, row, "方伟") == {}
    assert views.extract_article(conn, settings, row, "方伟", [{"id": 1, "name_zh": "腾讯"}]) == 0
    assert conn.statements == []


def test_reply_summary_uses_owner_text_and_linked_question_without_guest_images(monkeypatch):
    group_id, owner_id = TRUSTED_OWNERS["fangwei-zsxq"]
    identity = {
        "version": 1, "source_id": "fangwei-zsxq", "group_id": group_id,
        "owner_account_id": owner_id, "poster_account_id": "guest-id", "kind": "owner_reply_context",
        "replies": [{
            "comment_id": "reply-1", "parent_comment_id": "question-1", "author_account_id": owner_id,
            "text": "腾讯的广告收入增长来自转化效率改善。", "question": "广告为什么增长？",
            "question_account_id": "guest-id", "question_comment_id": "question-1",
        }],
    }
    seen = []

    def chat(settings, messages, **kwargs):
        seen.append(messages[1]["content"])
        return '{"summary":"广告转化效率改善","points":[],"tags":[]}', "offline-stub"

    def forbidden(*args, **kwargs):
        raise AssertionError("guest image/chunk cache was read")

    monkeypatch.setattr(digest, "chat", chat)
    monkeypatch.setattr(digest, "article_ocr", forbidden)
    monkeypatch.setattr(digest, "original_chunks", forbidden)
    row = article({"zsxq_identity": identity, "poster_role": "guest"})
    row = project_article(row)
    row["raw_meta"]["zsxq_identity"]["projection_md"] = row["body_md"]
    digest.digest_article(Connection(), SimpleNamespace(display_timezone="Asia/Shanghai"), row, "方伟")
    assert len(seen) == 1
    assert "转化效率改善" in seen[0] and "仅作星主回复上下文" in seen[0]
    assert "Guest advertising" not in seen[0] and "Untrusted guest title" not in seen[0]


def test_rejected_chunk_rebuild_preserves_raw_and_existing_chunks():
    conn = Connection({"source_id": "fangwei-zsxq", "raw_meta": {"poster_id": "guest"}})
    assert chunks.replace_chunks(conn, 7, "title", "guest body") == 0
    assert len(conn.statements) == 1
    assert conn.statements[0][0].startswith("SELECT")


def test_missing_source_execution_context_fails_closed(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unknown source was processed")

    monkeypatch.setattr(digest, "chat", forbidden)
    monkeypatch.setattr(views, "chat", forbidden)
    row = {"id": 7, "body_text": "unverified guest body", "raw_meta": {"poster_id": "guest"}}
    conn = Connection()
    settings = SimpleNamespace(display_timezone="Asia/Shanghai")
    assert digest.digest_article(conn, settings, row, "方伟") == {}
    assert views.extract_article(conn, settings, row, "方伟", [{"id": 1, "name_zh": "腾讯"}]) == 0
    assert conn.statements == []


def test_legacy_owner_model_jobs_wait_for_persisted_projection(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy model job would generate an unusable cache")

    monkeypatch.setattr(digest, "chat", forbidden)
    monkeypatch.setattr(views, "chat", forbidden)
    _group, owner = TRUSTED_OWNERS["fangwei-zsxq"]
    row = article({"poster_id": owner, "poster_role": "guest"})
    conn = Connection()
    settings = SimpleNamespace(display_timezone="Asia/Shanghai")
    assert digest.digest_article(conn, settings, row, "方伟") == {}
    assert views.extract_article(conn, settings, row, "方伟", [{"id": 1, "name_zh": "腾讯"}]) == 0
    assert conn.statements == []


def test_theses_without_complete_input_provenance_stay_hidden():
    assert theses.present({"theses": [{"title": "unproven synthesis"}]}, []) == []
    assert theses.present({"theses": [], "views_hash": "old-input-set"}, []) == []


def test_hidden_or_suppressed_direct_execution_never_calls_model_or_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("hidden content reached processing")

    monkeypatch.setattr(digest, "chat", forbidden)
    monkeypatch.setattr(digest, "article_ocr", forbidden)
    monkeypatch.setattr(views, "chat", forbidden)
    monkeypatch.setattr(views, "article_ocr", forbidden)
    settings = SimpleNamespace(display_timezone="Asia/Shanghai")
    for key in ("hidden", "suppressed"):
        for source_id in ("notes", "fangwei-zsxq"):
            row = article({key: "held"})
            row["source_id"] = source_id
            if source_id == "fangwei-zsxq":
                group, owner = TRUSTED_OWNERS[source_id]
                row["raw_meta"]["zsxq_identity"] = {
                    "version": 1, "source_id": source_id, "group_id": group,
                    "owner_account_id": owner, "poster_account_id": owner,
                    "kind": "owner_post", "replies": [], "projection_md": row["body_md"],
                }
            conn = Connection()
            assert digest.digest_article(conn, settings, row, "方伟") == {}
            assert views.extract_article(conn, settings, row, "方伟", [{"id": 1, "name_zh": "腾讯"}]) == 0
            assert conn.statements == []


def test_candidate_and_thesis_inputs_share_public_visibility_and_interpolate_sql():
    from researchlib.empty_answer import visible_sql

    class CandidateConnection(Connection):
        def fetchall(self):
            return []

    conn = CandidateConnection()
    settings = SimpleNamespace(display_timezone="Asia/Shanghai")
    assert digest.rows_needing_digest(conn, [7]) == []
    assert views.pending_articles(conn, [1], ["notes"], 10) == []
    assert theses.views_for(conn, 1, {"notes": SimpleNamespace(author="作者")}, settings) == []
    assert theses.companies_with_views(conn, ["notes"]) == []
    for sql, _params in conn.statements:
        assert visible_sql("a") in sql
        assert "{model_cache_safe_sql" not in sql and "{visible_sql" not in sql and "{not_guest_sql" not in sql

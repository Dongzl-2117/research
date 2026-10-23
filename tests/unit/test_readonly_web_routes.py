from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData
from starlette.requests import Request

from researchlib.config import Settings
from researchlib.sources import Source
from researchlib.web import routes
from researchlib.web.queries import Scope


def request_for(settings, method="GET", path="/", query=""):
    state = SimpleNamespace(settings=settings, sources_mtime=None)
    return Request({
        "type": "http", "method": method, "path": path,
        "query_string": query.encode(), "headers": [],
        "app": SimpleNamespace(state=state), "scheme": "http",
        "server": ("testserver", 80), "client": ("testclient", 1),
    })


@pytest.fixture
def web_settings(tmp_path):
    return Settings.from_env({
        "DATABASE_URL": "postgresql://unused/never_connect",
        "DATA_DIR": str(tmp_path / "data"),
        "SOURCES_FILE": str(tmp_path / "sources.yaml"),
    })


class ConnectionSpy:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append(sql)
        return self

    def rollback(self):
        self.calls.append("rollback")

    def commit(self):
        self.calls.append("commit")

    def close(self):
        self.calls.append("close")


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_read_request_guard_precedes_helpers_and_never_commits(monkeypatch, web_settings, method):
    conn = ConnectionSpy()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(routes, "connect", lambda settings: conn)
    monkeypatch.setattr(routes, "ensure_search_session", lambda c: c.execute("SELECT extension_state"))
    with routes.database(request_for(web_settings, method)) as current:
        current.execute("SELECT stored_content")
    assert conn.calls == [
        "SET TRANSACTION READ ONLY", "SELECT extension_state", "SELECT stored_content", "rollback", "close",
    ]


def test_explicit_post_retains_write_transaction(monkeypatch, web_settings):
    conn = ConnectionSpy()
    monkeypatch.setattr(routes, "connect", lambda settings: conn)
    monkeypatch.setattr(routes, "ensure_search_session", lambda c: None)
    with routes.database(request_for(web_settings, "POST")) as current:
        current.execute("INSERT explicit_user_action")
    assert conn.calls == ["INSERT explicit_user_action", "commit", "close"]


def test_read_transaction_closes_on_helper_failure(monkeypatch, web_settings):
    conn = ConnectionSpy()
    monkeypatch.setattr(routes, "connect", lambda settings: conn)

    def fail(_conn):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(routes, "ensure_search_session", fail)
    with pytest.raises(RuntimeError, match="unavailable"), routes.database(request_for(web_settings)):
        pass
    assert conn.calls == ["SET TRANSACTION READ ONLY", "rollback", "close"]


def test_source_reload_only_changes_in_memory_cache(monkeypatch, web_settings):
    import researchlib.sources as sources_module

    web_settings.sources_path.write_text(
        "sources:\n  - id: notes\n    kind: markdown\n    name: 笔记\n    author: 作者\n    enabled: true\n"
    )
    forbidden = []
    monkeypatch.setattr(sources_module, "sync_sources", lambda *a, **k: forbidden.append("sync"))
    conn = ConnectionSpy()
    request = request_for(web_settings)
    assert [source.id for source in routes.active_sources(request, conn)] == ["notes"]
    assert [source.id for source in routes.active_sources(request, conn)] == ["notes"]
    assert conn.calls == [] and forbidden == []


def test_cards_display_stored_digest_without_scheduling(monkeypatch, web_settings):
    import researchlib.digest as digest

    queued = []
    monkeypatch.setattr(digest, "schedule_digests", lambda *a, **k: queued.append(a))
    monkeypatch.setattr(routes.queries, "article_tags", lambda *a, **k: {})
    monkeypatch.setattr(routes.queries, "digests_for", lambda *a: {1: {"summary": "已存摘要", "points": []}})
    cards = [{"id": 1, "source_id": "notes", "plain_title": "题目", "author": "作者", "summary": ""},
             {"id": 2, "source_id": "notes", "plain_title": "无摘要", "author": "作者", "summary": ""}]
    result = routes._decorate(None, cards, web_settings)
    assert result[0]["summary"] == "已存摘要"
    assert queued == []


def test_company_views_do_not_queue_processing(monkeypatch, web_settings):
    import researchlib.views as views

    forbidden = []
    monkeypatch.setattr(views, "queue_for_company", lambda *a, **k: forbidden.append("views"))
    monkeypatch.setattr(routes.thesis_map, "needs_build", lambda *a, **k: True)
    monkeypatch.setattr(routes.thesis_map, "schedule", lambda *a, **k: forbidden.append("theses"))
    monkeypatch.setattr(routes.thesis_map, "stored", lambda *a: None)
    monkeypatch.setattr(routes.thesis_map, "present", lambda *a: [])
    monkeypatch.setattr(routes, "view_timeline", lambda *a: [])
    monkeypatch.setattr(routes, "count_view_pending", lambda *a: 3)
    result = routes._company_views(request_for(web_settings), None, {"id": 1, "name_zh": "腾讯"}, {}, "")
    assert result["pending"] == 3
    assert forbidden == []


def test_article_html_get_neither_rewrites_ocr_nor_queues_digest(monkeypatch, web_settings):
    import researchlib.digest as digest
    import researchlib.ocr as ocr

    forbidden = []
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ocr, "refresh_article_ocr", lambda *a: forbidden.append("ocr"))
    monkeypatch.setattr(digest, "schedule_digests", lambda *a, **k: forbidden.append("digest"))
    source = Source("notes", "笔记", "作者", "markdown", True)
    article = {"id": 1, "source_id": source.id, "title": "题目", "body_md": "存储正文", "body_text": "存储正文",
               "raw_meta": {}, "duplicate_of": None, "content_hash": "hash", "ocr_hash": "old",
               "published_at": datetime(2026, 10, 23, tzinfo=UTC)}

    @contextmanager
    def fake_database(_request):
        yield None

    monkeypatch.setattr(routes, "database", fake_database)
    monkeypatch.setattr(routes, "active_sources", lambda *a: [source])
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: article)
    monkeypatch.setattr(routes.queries, "article_images", lambda *a: [])
    monkeypatch.setattr(routes.queries, "digests_for", lambda *a: {})
    monkeypatch.setattr(routes.queries, "image_ocr_texts", lambda *a: {})
    monkeypatch.setattr(routes.queries, "sample_state", lambda *a: False)
    monkeypatch.setattr(routes.queries, "get_translation", lambda *a: None)
    monkeypatch.setattr(routes.queries, "same_article_elsewhere", lambda *a: [])
    monkeypatch.setattr(routes.revision_log, "summary", lambda *a: {})
    monkeypatch.setattr(routes, "same_period", lambda *a: [])
    monkeypatch.setattr(routes, "clean_article", lambda c, aid, sid, body: body)
    monkeypatch.setattr(routes, "render_page", lambda req, name, context, **kwargs: context)
    result = routes.article_page(request_for(web_settings, path="/articles/1"), 1)
    assert "存储正文" in result["body_html"]
    assert result["digest_summary"] == "" and forbidden == []


def test_web_lifespan_has_no_database_or_upkeep_side_effect(monkeypatch, web_settings):
    import researchlib.ask as ask
    import researchlib.db as db
    import researchlib.duplicates as duplicates
    import researchlib.mentions as mentions
    from researchlib.web.app import lifespan

    forbidden = []
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(db, "prepare", lambda *a: forbidden.append("prepare"))
    monkeypatch.setattr(ask, "mark_interrupted", lambda *a: forbidden.append("mark"))
    monkeypatch.setattr(duplicates, "dedupe_if_needed", lambda *a: forbidden.append("dedupe"))
    monkeypatch.setattr(mentions, "rebuild_if_stale", lambda *a: forbidden.append("mentions"))

    async def run():
        async with lifespan(SimpleNamespace(state=SimpleNamespace(settings=web_settings))):
            assert forbidden == []

    asyncio.run(run())
    assert forbidden == []


def scope_sources():
    return [Source("a", "来源甲", "方伟", "markdown", True),
            Source("b", "来源乙", "方伟", "markdown", True),
            Source("c", "来源丙", "另一作者", "markdown", True)]


def test_saved_scope_recovers_exact_source_and_author_without_query_override():
    restored = routes._saved_scope_context({"source_ids": ["a"], "date_from": "2026-01-02"}, scope_sources())
    assert restored["filters"]["author"] == "方伟"
    assert restored["filters"]["source"] == "a"
    assert restored["filters"]["from"] == "2026-01-02"
    assert restored["saved_source_ids"] == ["a"]
    assert not restored["saved_scope_unknown"]


def test_saved_arbitrary_subset_is_preserved_not_replaced_with_all_sources():
    sources = scope_sources()
    restored = routes._saved_scope_context({"source_ids": ["a", "c"]}, sources)
    assert restored["filters"]["author"] == restored["filters"]["source"] == ""
    form = FormData([("saved_scope_present", "1"), ("saved_scope_author", ""), ("saved_scope_source", ""),
                     ("saved_sources", "a"), ("saved_sources", "c")])
    selected = routes._preserve_saved_scope(form, {"author": "", "source": ""}, sources,
                                            Scope(source_ids=["a", "b", "c"]))
    assert selected.source_ids == ["a", "c"]


@pytest.mark.parametrize("stored_ids", [[], ["gone"], ["a", "gone"]])
def test_unknown_saved_scope_never_silently_expands(stored_ids):
    sources = scope_sources()
    restored = routes._saved_scope_context({"source_ids": stored_ids}, sources)
    assert restored["saved_scope_unknown"]
    form = FormData([("saved_scope_present", "1"), *[("saved_sources", sid) for sid in stored_ids]])
    with pytest.raises(HTTPException) as error:
        routes._preserve_saved_scope(form, {"author": "", "source": ""}, sources, Scope(source_ids=["a", "b", "c"]))
    assert error.value.status_code == 400


def test_saved_answer_with_excluded_reference_is_hidden_not_regenerated(monkeypatch):
    row = {"id": 1, "answer": "旧网友观点", "citations": [{"article_id": 7, "text": "不可信正文"}], "status": "done"}
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: None)
    result = routes._safe_saved_answer(None, row, scope_sources())
    assert result["answer"] == "" and result["citations"] == []
    assert result["status"] == "error" and result["cache_blocked"]
    assert row["answer"] == "旧网友观点"  # stored input remains intact


def test_saved_answer_with_unsafe_projection_is_hidden(monkeypatch):
    row = {"answer": "历史评论摘要", "citations": [{"article_id": 7}], "status": "done"}
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: {"source_id": "fangwei-zsxq"})
    monkeypatch.setattr(routes, "model_cache_safe_article", lambda *a: False)
    assert routes._safe_saved_answer(None, row, scope_sources())["answer"] == ""


def test_search_card_does_not_assign_unknown_poster_to_source_owner(web_settings):
    row = {"id": 1, "title": "题目", "published_at": None, "poster_role": "unknown"}
    card = routes._card(row, scope_sources()[0], web_settings)
    assert card["author"] == "身份未确认" and card["guest"]


def test_maintenance_only_runs_through_explicit_command(monkeypatch, web_settings):
    from researchlib import maintenance

    calls = []
    conn = ConnectionSpy()
    monkeypatch.setattr(maintenance.Settings, "from_env", lambda: web_settings)
    monkeypatch.setattr(maintenance, "prepare", lambda settings: calls.append("prepare") or conn)
    monkeypatch.setattr(maintenance, "mark_interrupted", lambda c: calls.append("recover"))
    monkeypatch.setattr(maintenance, "dedupe_if_needed", lambda settings: calls.append("dedupe"))
    monkeypatch.setattr(maintenance, "rebuild_if_stale", lambda settings: calls.append("mentions"))
    assert maintenance.main([]) == 0
    assert calls == ["prepare", "recover"] and conn.calls == ["commit", "close"]
    calls.clear()
    assert maintenance.main(["--upkeep"]) == 0
    assert calls == ["prepare", "recover", "dedupe", "mentions"]


def test_revisions_skip_unverified_historical_body_and_project_verified_owner():
    from researchlib import revisions

    current = {"id": 8, "source_id": "fangwei-zsxq", "title": "当前", "content_hash": "current",
               "published_at": None, "body_md": "当前星主正文", "body_text": "当前星主正文"}
    old = {"id": 1, "kind": "previous", "reason": "", "content_hash": "old", "title": "旧版",
           "published_at": None, "body_md": "可信正文\n\n## 评论里的回复\n\n伪造昵称评论",
           "body_text": "可信正文 伪造昵称评论", "raw_meta": {"poster_id": "1825524112"}, "created_at": None}
    unknown = {**old, "id": 2, "raw_meta": {"poster_name": "方伟", "poster_id": "unknown"},
               "body_md": "未知身份不能展示", "body_text": "未知身份不能展示"}

    class RevisionConnection:
        def execute(self, sql, params):
            assert "body_md" in sql and "raw_meta" in sql
            return self

        def fetchall(self):
            return [unknown, old]

    rows = revisions.versions(RevisionConnection(), current)
    assert len(rows) == 2
    assert rows[0]["unverified_history"] == 1
    assert rows[1]["body_text"] == "可信正文"
    assert "伪造昵称" not in rows[1]["body_text"]


def test_attachment_response_uses_manifest_and_download_headers(monkeypatch, web_settings, tmp_path):
    from researchlib.attachments import with_stored_attachments

    pdf = tmp_path / "报告.pdf"
    pdf.write_bytes(b"%PDF-test")
    meta = with_stored_attachments({}, [("报告.pdf", pdf)], web_settings.data_dir, "notes")
    entry = meta["local_attachments"]["报告.pdf"]
    filename = entry["storage_path"].rsplit("/", 1)[1]
    source = Source("notes", "笔记", "作者", "markdown", True)
    article = {"id": 1, "source_id": "notes", "raw_meta": meta, "body_md": "正文\n\n[报告](报告.pdf)"}

    @contextmanager
    def fake_database(_request):
        yield None

    monkeypatch.setattr(routes, "database", fake_database)
    monkeypatch.setattr(routes, "active_sources", lambda *a: [source])
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: article)
    response = routes.article_attachment(request_for(web_settings), 1, filename)
    assert response.media_type == "application/pdf"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    with pytest.raises(HTTPException):
        routes.article_attachment(request_for(web_settings), 1, "../../secret.env")


def test_excluded_article_cannot_download_attachment(monkeypatch, web_settings):
    @contextmanager
    def fake_database(_request):
        yield None

    monkeypatch.setattr(routes, "database", fake_database)
    monkeypatch.setattr(routes, "active_sources", lambda *a: scope_sources())
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: None)
    with pytest.raises(HTTPException) as error:
        routes.article_attachment(request_for(web_settings), 1, "a" * 64 + ".pdf")
    assert error.value.status_code == 404


@pytest.mark.parametrize("allowed", [False, True])
def test_direct_image_url_requires_an_eligible_article(monkeypatch, web_settings, allowed):
    image = web_settings.data_dir / "images" / "notes" / "p.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    source = Source("notes", "笔记", "作者", "markdown", True)

    class ImageConnection:
        def execute(self, sql, params):
            assert sql.startswith("SELECT") and params == ("notes/p.png", ["notes"])
            return self

        def fetchall(self):
            return [{"source_id": "notes", "raw_meta": {}, "body_md": "正文"}] if allowed else []

    @contextmanager
    def fake_database(_request):
        yield ImageConnection()

    monkeypatch.setattr(routes, "database", fake_database)
    monkeypatch.setattr(routes, "active_sources", lambda *a: [source])
    if allowed:
        assert routes.media(request_for(web_settings), "notes/p.png").path == image
    else:
        with pytest.raises(HTTPException) as error:
            routes.media(request_for(web_settings), "notes/p.png")
        assert error.value.status_code == 404


def test_saved_answer_without_citations_is_not_presented_as_verified():
    row = {"answer": "缺少出处的旧结果", "citations": [], "status": "done"}
    masked = routes._safe_saved_answer(None, row, scope_sources())
    assert masked["answer"] == "" and masked["cache_blocked"]


def test_saved_answer_http_get_restores_scope_and_warns_before_answer_without_processing(monkeypatch, web_settings):
    from fastapi.testclient import TestClient

    import researchlib.digest as digest
    from researchlib.answer_provenance import snapshot_input_provenance
    from researchlib.web import api as api_module
    from researchlib.web.app import create_app

    source = Source("gudujinhuati", "孤独进化体", "孤独进化体", "wechat_mp", True)
    stamp = datetime(2026, 10, 23, tzinfo=UTC)
    citation = {"n": 1, "article_id": 7, "ord": 1, "title": "原文", "text": "实际收入只有十元。",
                "author": source.author, "source_id": source.id, "date": "2026-10-23", "cited": True,
                "url": "/articles/7?chunk=1", "before": "", "after": "", "heading": ""}
    row = {"id": 1, "question": "已保存的问题", "scope": {"preset": True, "source_ids": [source.id]},
           "answer": "收入是100万元[1]。", "citations": [citation], "status": "done", "error": "",
           "model": "stored-model", "terms": [], "stats": {}, "created_at": stamp, "updated_at": stamp}
    current_article = {"id": 7, "source_id": source.id, "title": "原文", "body_md": "实际收入只有十元。",
                       "body_text": "实际收入只有十元。", "content_hash": "current", "ocr_hash": "", "raw_meta": {}}
    row["scope"].update(snapshot_input_provenance([current_article], [citation]))
    monkeypatch.setattr(routes, "_answer_input_articles", lambda *a: [current_article])
    conn = ConnectionSpy()
    processing = []
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(routes, "connect", lambda *a: conn)
    monkeypatch.setattr(routes, "ensure_search_session", lambda *a: None)
    monkeypatch.setattr(routes, "active_sources", lambda *a: [source])
    monkeypatch.setattr(api_module, "active_sources", lambda *a: [source])
    monkeypatch.setattr(routes.queries, "get_answer", lambda *a: row)
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: {"source_id": source.id, "raw_meta": {}})
    monkeypatch.setattr(routes.queries, "recent_answers", lambda *a: [])
    monkeypatch.setattr(routes.queries, "sample_state", lambda *a: False)
    monkeypatch.setattr(routes, "feedback_marks", lambda *a: {"irrelevant": set(), "missing": []})
    monkeypatch.setattr(routes, "llm_configured", lambda *a: False)
    monkeypatch.setattr(routes, "active_label", lambda *a: "LLM")
    monkeypatch.setattr(routes, "active_model_id", lambda *a: "")
    monkeypatch.setattr(routes, "start", lambda *a, **k: processing.append("ask"))
    monkeypatch.setattr(digest, "schedule_digests", lambda *a, **k: processing.append("digest"))
    with TestClient(create_app(web_settings)) as client:
        page = client.get("/ask/1?author=unrelated&source=unknown")
        payload = client.get("/api/v1/answers/1").json()
    assert page.status_code == 200
    assert 'name="saved_sources" value="gudujinhuati"' in page.text
    assert 'name="source" value="gudujinhuati"' in page.text
    assert page.text.index('class="checks"') < page.text.index('class="prose answer-body"')
    assert payload["scope"]["source_ids"] == [source.id] and payload["checks"]
    assert payload["answer"] == row["answer"]
    assert processing == []
    assert "commit" not in conn.calls
    assert conn.calls.count("SET TRANSACTION READ ONLY") == conn.calls.count("rollback") == 2


def test_pilot_legacy_json_cache_is_quarantined_without_deleting_or_regenerating(monkeypatch, web_settings):
    import json

    from researchlib.pilot import SUMMARY_FILE, VIEWS_FILE

    base = web_settings.data_dir / "pilot"
    base.mkdir(parents=True)
    view_path = base / VIEWS_FILE
    old_views = {"articles": {"7": [{"article_id": 7, "company_id": 1, "about": True,
                                    "judgment": "未知网友的旧模型输出"}]}}
    view_path.write_text(json.dumps(old_views))
    (base / SUMMARY_FILE).write_text(json.dumps({"腾讯": {"judgment": "未验证汇总"}}))

    @contextmanager
    def fake_database(_request):
        yield None

    monkeypatch.setattr(routes, "database", fake_database)
    monkeypatch.setattr(routes, "active_sources", lambda *a: scope_sources())
    monkeypatch.setattr(routes, "render_page", lambda req, name, context, **kw: context)
    result = routes.pilot_page(request_for(web_settings, path="/pilot"))
    assert result["pilot_notice"]
    assert all(group["cards"] == [] and group["summary"] is None for group in result["groups"])
    assert json.loads(view_path.read_text()) == old_views


def test_pilot_provenance_must_match_every_current_eligible_input(monkeypatch):
    proof = {"source_id": "a", "content_hash": "current"}
    views = {"articles": {"7": [{"article_id": 7}]}, "provenance": {"7": proof}}
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: {"source_id": "a", "content_hash": "current"})
    monkeypatch.setattr(routes, "model_cache_safe_article", lambda *a: True)
    assert routes._pilot_cache_current(None, views, scope_sources())
    views["provenance"]["7"] = {**proof, "content_hash": "old"}
    assert not routes._pilot_cache_current(None, views, scope_sources())
    views["provenance"]["7"] = proof
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: None)
    assert not routes._pilot_cache_current(None, views, scope_sources())


def test_pilot_summary_requires_same_provenance_and_no_removed_reference():
    proof = {"7": {"source_id": "a", "content_hash": "current"}}
    summary = {"sources": ["v7"], "provenance": proof, "judgment": "可核实的预览",
               "reasons": [{"text": "依据", "refs": ["v7"]}]}
    assert routes._pilot_summary(summary, {"provenance": proof}) is summary
    assert routes._pilot_summary({**summary, "sources": ["v99"]}, {"provenance": proof}) is None
    assert routes._pilot_summary({**summary, "provenance": None}, {"provenance": proof}) is None
    assert routes._pilot_summary({**summary, "reasons": [{"text": "混入历史内容", "refs": ["v99"]}]},
                                 {"provenance": proof}) is None


def test_direct_image_url_rejects_leftover_guest_comment_attachment(monkeypatch, web_settings):
    image = web_settings.data_dir / "images" / "fangwei-zsxq" / "orphan.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"retained raw image")
    source = Source("fangwei-zsxq", "方伟星球", "方伟", "zsxq", True)

    class ImageConnection:
        def execute(self, sql, params):
            assert "i.original_ref" in sql
            return self

        def fetchall(self):
            return [{"source_id": source.id, "raw_meta": {"poster_id": "1825524112"},
                     "body_md": "星主正文\n\n![](kept.png)", "original_ref": "orphan.png"}]

    @contextmanager
    def fake_database(_request):
        yield ImageConnection()

    monkeypatch.setattr(routes, "database", fake_database)
    monkeypatch.setattr(routes, "active_sources", lambda *a: [source])
    with pytest.raises(HTTPException) as error:
        routes.media(request_for(web_settings), "fangwei-zsxq/orphan.png")
    assert error.value.status_code == 404
    assert image.exists()  # Archive file is preserved.


def test_old_attachment_url_is_hidden_when_current_owner_body_no_longer_links_pdf(monkeypatch, web_settings, tmp_path):
    from researchlib.attachments import with_stored_attachments
    from researchlib.eligibility import TRUSTED_OWNERS

    pdf = tmp_path / "旧附件.pdf"
    pdf.write_bytes(b"%PDF-test")
    source = Source("fangwei-zsxq", "方伟星球", "方伟", "zsxq", True)
    _group, owner = TRUSTED_OWNERS[source.id]
    meta = with_stored_attachments({"poster_id": owner}, [("旧附件.pdf", pdf)], web_settings.data_dir, source.id)
    entry = meta["local_attachments"]["旧附件.pdf"]
    filename = entry["storage_path"].rsplit("/", 1)[1]
    article = {"id": 1, "source_id": source.id, "raw_meta": meta, "title": "星主正文",
               "body_md": "现有星主正文没有这个附件的链接。"}

    @contextmanager
    def fake_database(_request):
        yield None

    monkeypatch.setattr(routes, "database", fake_database)
    monkeypatch.setattr(routes, "active_sources", lambda *a: [source])
    monkeypatch.setattr(routes.queries, "get_article", lambda *a: article)
    with pytest.raises(HTTPException) as error:
        routes.article_attachment(request_for(web_settings), 1, filename)
    assert error.value.status_code == 404
    assert (web_settings.data_dir / "attachments" / entry["storage_path"]).exists()

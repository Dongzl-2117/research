"""审计 2–6：合成库中的媒体边界、公司交集、错误引用框及问库范围。"""
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import ask
from researchlib.experiments.capture import capture
from researchlib.experiments.schema import QuerySpec
from researchlib.experiments.store import Store
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import analyze, retrieve, search_articles
from researchlib.sources import Source, sync_sources
from researchlib.web.app import create_app
from researchlib.web.experiments import create_experiment_app
from researchlib.web.queries import Scope


def setup_sources(conn, settings, tmp_path):
    sources = [Source("audit-a", "虚构札记甲", "王小明", "markdown", True),
               Source("audit-b", "虚构札记乙", "李四", "markdown", True)]
    path = tmp_path / "audit-sources.yaml"
    path.write_text(yaml.safe_dump({"sources": [asdict(s) for s in sources]}, allow_unicode=True))
    sync_sources(conn, sources)
    conn.commit()
    return replace(settings, sources_path=path), sources


def article(conn, source, title="图表札记", body="用于审计的合成原文。", meta=None):
    row = conn.execute(
        "INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text,raw_meta)"
        " VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (source, title, "synthetic-" + title, title, body, body, Jsonb(meta or {})),
    ).fetchone()
    conn.commit()
    return row["id"]



def store_text(conn, settings, source, external, title, body, when=None):
    store_documents(conn, source=source, docs=[ParsedDoc(
        external, title, when or datetime(2026, 1, 1, tzinfo=UTC), body, {}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    return conn.execute(
        "SELECT id FROM articles WHERE source_id=%s AND external_id=%s", (source.id, external),
    ).fetchone()["id"]


def link_image(conn, article_id, source_id, storage_path):
    conn.execute(
        "INSERT INTO article_images(article_id,source_id,original_ref,storage_path,content_hash)"
        " VALUES(%s,%s,%s,%s,%s)",
        (article_id, source_id, storage_path, storage_path, "synthetic-image"),
    )
    conn.commit()


def image_file(settings, path, data):
    target = settings.data_dir / "images" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_svg_is_sandboxed_and_raster_still_renders(conn, settings, tmp_path):
    settings, _ = setup_sources(conn, settings, tmp_path)
    aid = article(conn, "audit-a")
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg"><script>top.compromised=true</script>'
           b'<rect width="10" height="10"/></svg>')
    for path, data in (("remote/a.svg", svg), ("audit-a/a.png", b"\x89PNG\r\n\x1a\n"),
                       ("remote/a.html", b"<script>top.compromised=true</script>")):
        link_image(conn, aid, "audit-a", path)
        image_file(settings, path, data)
    with TestClient(create_app(settings)) as client:
        for path in ("remote/a.svg", "audit-a/a.png"):
            response = client.get("/media/" + path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("image/")
            assert "attachment" not in response.headers.get("content-disposition", "")
            csp = response.headers["content-security-policy"]
            assert "sandbox;" in csp and "script-src 'none'" in csp and "default-src 'none'" in csp
            assert "allow-scripts" not in csp and "allow-same-origin" not in csp
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "private, no-store"
        download = client.get("/media/remote/a.html")
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("disabled", [True, False])
def test_media_source_is_checked_on_each_request_and_shared_image_survives(conn, settings, tmp_path, disabled):
    settings, sources = setup_sources(conn, settings, tmp_path)
    first = article(conn, "audit-a", "甲文章")
    second = article(conn, "audit-b", "乙文章")
    shared, private = "remote/shared.png", "audit-a/private.png"
    for path in (shared, private):
        image_file(settings, path, b"\x89PNG\r\n\x1a\n")
        link_image(conn, first, "audit-a", path)
    link_image(conn, second, "audit-b", shared)
    with TestClient(create_app(settings)) as client:
        before = client.get("/media/" + private)
        assert before.status_code == 200
        current = [replace(sources[0], enabled=False), sources[1]] if disabled else [sources[1]]
        settings.sources_path.write_text(yaml.safe_dump({"sources": [asdict(s) for s in current]}))
        # 包括旧缓存校验头及 Range，都必须重新过来源检查。
        denied = client.get("/media/" + private, headers={
            "If-None-Match": before.headers["etag"], "Range": "bytes=0-2",
        })
        assert denied.status_code == 404
        assert client.get("/media/" + shared).status_code == 200
        conn.execute("UPDATE articles SET raw_meta=%s WHERE id=%s", (Jsonb({"poster_role": "guest"}), second))
        conn.commit()
        assert client.get("/media/" + shared).status_code == 404
        assert conn.execute("SELECT count(*) AS n FROM article_images").fetchone()["n"] == 3


def test_hashed_media_is_cached_by_the_browser_only(conn, settings, tmp_path):
    # 按内容哈希命名的图同一个地址永远同一张：读者浏览器缓存一年（private，Cloudflare 不存，审核 O1）。
    # 看不到的照旧 404、不缓存；不是哈希名字的照旧不缓存。
    settings, _ = setup_sources(conn, settings, tmp_path)
    shown = article(conn, "audit-a", "看得到")
    hidden = article(conn, "audit-a", "网友帖", meta={"poster_role": "guest"})
    digest = "ab" + "0" * 62
    shown_path, hidden_path = f"remote/ab/{digest}.png", f"audit-a/cd/{'cd' + '1' * 62}.png"
    for aid, path in ((shown, shown_path), (hidden, hidden_path)):
        image_file(settings, path, b"\x89PNG\r\n\x1a\n")
        link_image(conn, aid, "audit-a", path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/media/" + shown_path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert "content-encoding" not in response.headers
        denied = client.get("/media/" + hidden_path)
        assert denied.status_code == 404 and denied.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("title,meta,visible", [
    ("网友帖", {"poster_role": "guest"}, False),
    ("「图片」 评论（2）", {"poster_role": "owner"}, False),
    (" 「图片」 评论（2）", {"poster_role": "owner"}, False),
    ("\t「图片」 评论（2）", {"poster_role": "owner"}, False),
    ("\n「图片」 评论（2）", {"poster_role": "owner"}, False),
    ("「图片」 评论（两条）", {"poster_role": "owner"}, True),
    ("短帖直达", {"hidden": "short_post"}, True),
    ("普通文章", {}, True),
])
def test_image_visibility_matches_direct_article_policy(conn, settings, tmp_path, title, meta, visible):
    settings, _ = setup_sources(conn, settings, tmp_path)
    aid = article(conn, "audit-a", title, meta=meta)
    path = "audit-a/chart.png"
    image_file(settings, path, b"\x89PNG\r\n\x1a\n")
    link_image(conn, aid, "audit-a", path)
    with TestClient(create_app(settings)) as client:
        # 短帖按既有规则允许直达，不能因为图片保护而把它的图弄坏。
        assert client.get(f"/articles/{aid}").status_code == (200 if visible else 404)
        assert client.get("/media/" + path).status_code == (200 if visible else 404)
        if not visible:
            shared = article(conn, "audit-b", "合法共享引用")
            link_image(conn, shared, "audit-b", path)
            assert client.get("/media/" + path).status_code == 200


def test_unreferenced_or_deleted_media_and_path_escape_are_denied(conn, settings, tmp_path):
    settings, _ = setup_sources(conn, settings, tmp_path)
    path = "audit-a/unlinked.png"
    image_file(settings, path, b"unlinked")
    with TestClient(create_app(settings)) as client:
        assert client.get("/media/" + path).status_code == 404
        aid = article(conn, "audit-a")
        link_image(conn, aid, "audit-a", path)
        assert client.get("/media/" + path).status_code == 200
        conn.execute("DELETE FROM articles WHERE id=%s", (aid,))
        conn.commit()
        assert client.get("/media/" + path).status_code == 404
        for escaped in ("%2e%2e/other.png", "%2fetc/passwd", "%5cetc/passwd"):
            assert client.get("/media/" + escaped).status_code == 404


def seed_retrieval(conn, settings, source):
    for name, title, body in (
        ("a", "苹果经营札记", "苹果的产品收入与服务收入结构。" * 30),
        ("b", "腾讯经营札记", "腾讯的广告收入与服务收入结构。" * 30),
        ("both", "苹果和腾讯对照", "苹果与腾讯的收入结构和经营机制对照。" * 30),
    ):
        store_documents(conn, source=source, docs=[ParsedDoc(
            name, title, datetime(2026, 1, 1, tzinfo=UTC), body, {}, [],
        )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()


@pytest.mark.parametrize("purpose", ["display", "llm", "deep", "search"])
def test_company_recall_keeps_explicit_company_intersection(conn, settings, tmp_path, purpose):
    settings, sources = setup_sources(conn, settings, tmp_path)
    seed_retrieval(conn, settings, sources[0])
    apple = conn.execute("SELECT id FROM companies WHERE name_zh='苹果'").fetchone()["id"]
    both = conn.execute("SELECT id FROM articles WHERE external_id='both'").fetchone()["id"]
    allowed = {r["article_id"] for r in conn.execute(
        "SELECT article_id FROM article_mentions WHERE company_id=%s", (apple,),
    ).fetchall()}
    scope = Scope(source_ids=[s.id for s in sources], company_id=apple)
    result = (search_articles(conn, "腾讯", scope, sources) if purpose == "search" else
              retrieve(conn, "腾讯", scope, sources, purpose=purpose))
    ids = {p.article_id for p in (result.hits if purpose == "search" else result.passages)}
    assert both in ids and ids <= allowed
    if purpose != "search":
        assert result.scope.company_id == apple


def test_uncertain_citation_keeps_exact_quote_box_in_article_page(conn, settings, tmp_path):
    settings, _ = setup_sources(conn, settings, tmp_path)
    prefix = "经营复盘关注渠道费用与现金流的持续变化以及利润结构，"
    wrong, right = prefix + "利润增长90%。", prefix + "利润下降80%。"
    aid = article(conn, "audit-a", body=wrong)
    with TestClient(create_app(settings)) as client:
        # 模拟存量块与现稿内容不一致，仍须展示引用原块，不能假装定位到相反结论。
        conn.execute("UPDATE article_chunks SET body=%s WHERE article_id=%s", (right, aid))
        conn.commit()
        page = client.get(f"/articles/{aid}?chunk=0")
        assert page.status_code == 200 and 'class="quoted"' in page.text
        assert right in page.text and 'class="is-quoted"' not in page.text


def test_experiment_scope_matches_capture_execution_state_and_saved_export(conn, settings, tmp_path):
    settings, sources = setup_sources(conn, settings, tmp_path)
    seed_retrieval(conn, settings, sources[0])
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id,external_id FROM articles").fetchall()}
    root, both = ids["a"], ids["both"]
    for key, source, when in (
        ("wrong-date", sources[0], datetime(2026, 1, 10, tzinfo=UTC)),
        ("wrong-tree", sources[0], datetime(2026, 1, 1, tzinfo=UTC)),
        ("wrong-source", sources[1], datetime(2026, 1, 1, tzinfo=UTC)),
    ):
        store_documents(conn, source=source, docs=[ParsedDoc(
            key, key + "苹果腾讯收入", when, (key + "苹果与腾讯的收入对照。") * 30, {}, [],
        )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id,external_id FROM articles").fetchall()}
    for key, aid in ids.items():
        if key == "wrong-tree":
            continue
        sid = "audit-b" if key == "wrong-source" else "audit-a"
        conn.execute("INSERT INTO doc_tree(source_id,article_id,parent_id,ord) VALUES(%s,%s,%s,0)",
                     (sid, aid, None if aid == root else root))
    conn.commit()
    apple = conn.execute("SELECT id FROM companies WHERE name_zh='苹果'").fetchone()["id"]
    spec = QuerySpec(id="scoped", query="腾讯 收入", scope={
        "source_ids": ["audit-a"], "company_id": apple, "tree_id": root,
        "date_from": "2026-01-01", "date_to": "2026-01-01",
    })
    snapshot = capture(settings.database_url, settings.sources_path, [spec])
    case = snapshot.cases[0]
    assert both in {r.article_id for r in case.rows}
    assert {r.article_id for r in case.rows + case.qa_rows + case.neighbours} <= {root, both}
    db = tmp_path / "experiments.sqlite3"
    Store.initialize(db, snapshot.fingerprint)
    app = create_experiment_app(snapshot, db, sources_path=settings.sources_path)
    with TestClient(app, base_url="http://127.0.0.1:8100", client=("127.0.0.1", 123)) as client:
        client.headers["x-experiment-csrf"] = app.state.experiment_csrf
        scope = spec.scope.model_dump(mode="json")
        assert client.get("/api/experiments/state").json()["cases"][0]["scope"] == scope
        run = client.post("/api/experiments/compare", json={"case_ids": [spec.id]}).json()
        assert run["cases"][0]["scope"] == scope
        exported = client.get(f"/api/experiments/runs/{run['id']}/export").json()
        assert exported["run"]["cases"][0]["scope"] == scope
        assert {p["article_id"] for p in run["cases"][0]["baseline"]["passages"]} <= {root, both}


@pytest.mark.parametrize("question", ["李四怎么看腾讯", "汇总李四的判断"])
def test_library_ask_other_author_stays_inside_that_author(conn, settings, tmp_path, monkeypatch, question):
    """整个资料库按原问库收窄到点名作者，不把正在看的文章自动补进来。"""
    settings, sources = setup_sources(conn, settings, tmp_path)
    seed_retrieval(conn, settings, sources[1])
    current = store_text(conn, settings, sources[0], "current-note", "当前文章", "这里是王小明谈苹果的资料。" * 30)
    seen = []

    def chat(settings, messages, **kwargs):
        seen.append(messages)
        return "李四的判断 [1]", "synthetic-model"

    monkeypatch.setattr(ask, "llm_configured", lambda _: True)
    monkeypatch.setattr(ask, "chat", chat)
    with TestClient(create_app(settings)) as client:
        rejected = client.post("/api/v1/ask", json={"question": question, "article": current, "chat": True})
        assert rejected.status_code == 400
        response = client.post("/api/v1/ask", json={"question": question, "wait": True})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "done" and seen
        assert payload["citations"] and all(p["source_id"] == "audit-b" for p in payload["citations"])
        assert all(p["article_id"] != current for p in payload["citations"])
        assert payload["scope"]["article_id"] is None
        assert any("虚构札记乙" in message["content"] for call in seen for message in call)
        assert all("库里其他文章" not in message["content"] for call in seen for message in call)


@pytest.mark.parametrize("batched", [False, True])
def test_article_ask_keeps_selected_article_when_question_names_another_author(
    conn, settings, tmp_path, monkeypatch, batched,
):
    """只问这篇不被问题里的另一作者换掉，也不再自动补库。"""
    settings, sources = setup_sources(conn, settings, tmp_path)
    seed_retrieval(conn, settings, sources[1])
    current = store_text(
        conn, settings, sources[0], "current-note", "当前判断", "王小明认为苹果的渠道成本稳定。" * 80,
    )
    seen = []

    def chat(settings, messages, **kwargs):
        seen.append(messages)
        return "合成比较 [1]", "synthetic-model"

    monkeypatch.setattr(ask, "llm_configured", lambda _: True)
    monkeypatch.setattr(ask, "chat", chat)
    if batched:
        monkeypatch.setattr(ask, "SINGLE_CALL_CHARS", 1000)
        monkeypatch.setattr(ask, "BATCH_CHARS", 600)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/ask", json={
            "question": "李四的判断与当前这篇有何不同？", "article": current, "wait": True,
        })
        payload = response.json()
        assert response.status_code == 200 and payload["status"] == "done", response.text
        assert payload["citations"] and {p["article_id"] for p in payload["citations"]} == {current}
        assert payload["scope"]["article_id"] == current
        assert any("王小明" in message["content"] and "虚构札记甲" in message["content"]
                   for call in seen for message in call)
        assert all("库里其他文章" not in message["content"] for call in seen for message in call)
        assert len(seen) > 1 if batched else len(seen) == 1


@pytest.mark.parametrize("filters", ["source", "author", "date", "empty-date", "company", "tree", "source-tree"])
def test_ask_preserves_explicit_filters(conn, settings, tmp_path, monkeypatch, filters):
    from researchlib.web.scoping import resolve_scope

    settings, sources = setup_sources(conn, settings, tmp_path)
    seed_retrieval(conn, settings, sources[1])
    current = store_text(conn, settings, sources[0], "current-note", "当前判断", "王小明认为苹果的渠道成本稳定。" * 30)
    apple = conn.execute("SELECT id FROM companies WHERE name_zh='苹果'").fetchone()["id"]
    both = conn.execute("SELECT id FROM articles WHERE external_id='both'").fetchone()["id"]
    for sid, aid, parent in (("audit-a", current, None), ("audit-b", both, current)):
        conn.execute("INSERT INTO doc_tree(source_id,article_id,parent_id,ord) VALUES(%s,%s,%s,0)", (sid, aid, parent))
    conn.commit()
    options = {
        "source": {"source": "audit-b"}, "author": {"author": "李四"},
        "date": {"from": "2026-01-01", "to": "2026-01-01"}, "empty-date": {"from": "2026-02-01"},
        "company": {"company": apple}, "tree": {"tree": current},
        "source-tree": {"source": "audit-b", "tree": current},
    }[filters]
    scope, _ = resolve_scope({key: str(value) for key, value in options.items()}, sources)
    from researchlib.web.queries import scope_clause
    where, params = scope_clause(scope)
    allowed = {r["id"] for r in conn.execute(f"SELECT a.id FROM articles a WHERE {where}", params).fetchall()}
    calls = []

    def chat(settings, messages, **kwargs):
        calls.append(messages)
        return "合成回答 [1]", "synthetic-model"

    monkeypatch.setattr(ask, "llm_configured", lambda _: True)
    monkeypatch.setattr(ask, "chat", chat)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/ask", json={"question": "收入结构", "wait": True, **options})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert {p["article_id"] for p in payload["citations"]} <= allowed
        if payload["citations"]:
            assert all(payload["scope"][key] == value for key, value in ask.scope_json(scope).items())
        if not allowed:
            assert payload["id"] is None and payload["citations"] == [] and not calls
        else:
            assert payload["status"] == "done" and calls
            assert payload["citations"]


@pytest.mark.parametrize("other_enabled", [True, False])
def test_queued_ask_intersects_saved_scope_with_live_sources(
    conn, settings, tmp_path, monkeypatch, other_enabled,
):
    settings, sources = setup_sources(conn, settings, tmp_path)
    seed_retrieval(conn, settings, sources[1])
    private = "王小明的旧来源专属正文，苹果渠道收入稳定。"
    current = store_text(conn, settings, sources[0], "queued-current", "排队前的当前文章", private * 30)
    both = conn.execute("SELECT id FROM articles WHERE external_id='both'").fetchone()["id"]
    apple = conn.execute("SELECT id FROM companies WHERE name_zh='苹果'").fetchone()["id"]
    for sid, aid, parent in (("audit-a", current, None), ("audit-b", both, current)):
        conn.execute("INSERT INTO doc_tree(source_id,article_id,parent_id,ord) VALUES(%s,%s,%s,0)", (sid, aid, parent))
    conn.commit()
    scopes, model_calls, submitted = [], [], []

    def submit(*args, **kwargs):
        submitted.append((args, kwargs))

    def chat(settings, messages, **kwargs):
        model_calls.append(messages)
        return "剩余授权资料 [1]", "synthetic-model"

    real_retrieve = ask.retrieve

    def capture(conn, question, scope, sources, **kwargs):
        scopes.append((kwargs.get("purpose"), scope))
        return real_retrieve(conn, question, scope, sources, **kwargs)

    monkeypatch.setattr(ask, "_inline", lambda: False)
    monkeypatch.setattr(ask._executor, "submit", submit)
    monkeypatch.setattr(ask, "retrieve", capture)
    monkeypatch.setattr(ask, "llm_configured", lambda _: True)
    monkeypatch.setattr(ask, "chat", chat)
    with TestClient(create_app(settings)) as client:
        pending = client.post("/api/v1/ask", json={
            "question": "苹果收入", "company": apple, "from": "2026-01-01", "to": "2026-01-01",
            "tree": current, "wait": False,
        }).json()
    assert pending["status"] == "running" and len(submitted) == 1 and not model_calls
    live = [replace(sources[0], enabled=False), replace(sources[1], enabled=other_enabled)]
    settings.sources_path.write_text(yaml.safe_dump({"sources": [asdict(s) for s in live]}, allow_unicode=True))
    sync_sources(conn, live)
    conn.commit()
    marked = len(scopes)
    args, kwargs = submitted.pop()
    args[0](*args[1:], conn=conn, **kwargs)
    saved = conn.execute("SELECT status,citations FROM llm_answers WHERE id=%s", (pending["id"],)).fetchone()
    job_scopes = [scope for purpose, scope in scopes[marked:] if purpose == "deep"]
    assert all(private not in message["content"] for call in model_calls for message in call)
    if other_enabled:
        assert job_scopes and all(scope.source_ids == ["audit-b"] for scope in job_scopes)
        assert all((scope.company_id, scope.date_from.isoformat(), scope.date_to.isoformat(), scope.tree_id) ==
                   (apple, "2026-01-01", "2026-01-01", current) for scope in job_scopes)
        assert saved["status"] == "done" and model_calls
        assert saved["citations"] and all(item["article_id"] != current for item in saved["citations"])
        assert all(item["source_id"] == "audit-b" for item in saved["citations"])
    else:
        assert job_scopes == [] and not model_calls
        assert saved["status"] == "error" and not saved["citations"]


@pytest.mark.parametrize("reference", ["与当前这篇", ""])
@pytest.mark.parametrize("topic", [
    "日本文化产业", "脚本文件安全", "样本文档管理", "基本文本处理", "日本文", "合同本文",
])
def test_comparison_reference_cannot_remove_characters_inside_topic(
    conn, settings, tmp_path, topic, reference,
):
    settings, sources = setup_sources(conn, settings, tmp_path)
    baseline = analyze(conn, f"李四对{topic}的判断", sources)
    question = f"李四对{topic}的判断{reference}有何不同？"
    query = analyze(conn, question, sources)
    topic_words = {t.text for t in baseline.content_terms}
    assert topic_words and topic_words <= {t.text for t in query.content_terms}
    assert query.raw == question and query.authors == ["李四"] and not query.overview


@pytest.mark.parametrize("reference", ["与当前这篇", ""])
@pytest.mark.parametrize("topic,damaged", [("日本文化产业", "日 化产业"), ("脚本文件安全", "脚 件安全")])
def test_comparison_retrieves_original_topic_instead_of_its_damaged_spelling(
    conn, settings, tmp_path, topic, damaged, reference,
):
    settings, sources = setup_sources(conn, settings, tmp_path)
    for key, text in (("original-topic", topic), ("damaged-topic", damaged)):
        store_documents(conn, source=sources[1], docs=[ParsedDoc(
            key, text + "合成研究", datetime(2026, 1, 1, tzinfo=UTC),
            (text + "需要研究经营机制和不同之处。") * 30, {}, [],
        )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    identities = {r["external_id"]: r["id"] for r in conn.execute("SELECT id,external_id FROM articles").fetchall()}
    question = f"李四对{topic}的判断{reference}有何不同？"
    scope = Scope(source_ids=[sources[1].id])
    found = search_articles(conn, question, scope, sources)
    assert found.hits and found.hits[0].article_id == identities["original-topic"]
    assert found.hits[0].full
    assert not any(h.full and h.article_id == identities["damaged-topic"] for h in found.hits)
    recalled = retrieve(conn, question, scope, sources, purpose="llm")
    assert recalled.passages and recalled.passages[0].article_id == identities["original-topic"]


@pytest.mark.parametrize("question", ["李四 和 本文 比较一下。", "李四与本文的判断有何不同？"])
def test_standalone_article_reference_still_allows_metadata_comparison(conn, settings, tmp_path, question):
    settings, sources = setup_sources(conn, settings, tmp_path)
    query = analyze(conn, question, sources)
    assert query.overview and not query.content_terms and query.authors == ["李四"]

"""独立测试库中的译制、回执、页面、只读计划和失败保留；模型为固定替身。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate import audit, qa, service
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.provenance import AlignmentError
from researchlib.web.app import create_app

BODY = "<p>First paragraph 10 dollars.</p><p>Second paragraph 20 dollars.</p>"


def article(conn, settings, body=BODY):
    source = next(s for s in load_sources(settings.sources_path) if s.id == "stratechery")
    source = replace(source, min_chars=0)  # 缩短的合成夹具仍须进入待译选择器，不能靠短帖隐藏假通过。
    store_documents(conn, source=source, docs=[ParsedDoc(
        "synthetic-bilingual", "Synthetic audit", datetime(2026, 1, 1, tzinfo=UTC), body,
        {"format": "markdown", "language": "en"}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    return conn.execute("SELECT id FROM articles WHERE external_id = 'synthetic-bilingual'").fetchone()["id"]


def verifier(user):
    """独立核验替身：合成片段各交一条原文和译文都在的引用。"""
    source, target = user["source"], user["target"]
    pairs = [
        ("First paragraph 10 dollars", "第一段10美元", "First paragraph", "第一段"),
        ("Second paragraph 20 dollars", "第二段20美元", "Second paragraph", "第二段"),
        ("In the 1990s", "20世纪90年代", "Microsoft", "微软"),
        ("Synthetic audit", "合成审计", "Synthetic audit", "合成审计"),
    ]
    claims = [
        {
            "source": en, "target": zh,
            "object_source": obj_en, "object_target": obj_zh,
            "value": "", "relation": "", "same": True,
        }
        for en, zh, obj_en, obj_zh in pairs if en in source and zh in target
    ]
    return {
        "evidence_id": user["evidence_id"],
        "verdict": "equivalent" if claims else "uncertain",
        "claims": claims,
    }


def model(**kwargs):
    user = kwargs["user"]
    if user.get("verify"):
        return ChatJsonResult(data=verifier(user), model="synthetic-model", usage=None, latency_ms=0)
    values = []
    for identity, source_hash, html in zip(user["segment_ids"], user["source_hashes"], user["segments"], strict=True):
        translated = html.replace("Synthetic audit", "合成审计")
        translated = translated.replace("First paragraph ", "第一段").replace("Second paragraph ", "第二段")
        translated = translated.replace(" dollars", "美元").replace("source: member-rss", "归档头")
        values.append({"id": identity, "source_hash": source_hash, "html": translated})
    return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)


def snapshot(conn, article_id):
    return dict(conn.execute(
        "SELECT body_html, provenance, complete, updated_at FROM article_translations WHERE article_id=%s",
        (article_id,),
    ).fetchone())


def test_real_write_jsonb_page_and_qa_use_the_same_mapping(conn, settings, monkeypatch):
    article_id = article(conn, settings)
    monkeypatch.setattr(service, "chat_json", model)
    result = service.translate_article(conn, article_id, data_dir=settings.data_dir)
    row = snapshot(conn, article_id)
    assert result.status == "translated" and row["complete"]
    assert row["provenance"]["model"] == "synthetic-model"
    assert len(row["provenance"]["segments"]) == 2
    assert conn.execute("SELECT count(*) AS n FROM article_chunks WHERE article_id=%s AND lang='zh'",
                        (article_id,)).fetchone()["n"] > 0
    report = qa.check_article(conn, settings, article_id)
    assert report.correspondence == "mapped" and report.status == "pass"
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}").text
        assert page.count('class="bilingual-pair"') == report.pairs == 2
        assert "旧译文未记录源段映射" not in page
        assert "第一段10美元" in page and "第二段20美元" in page
        chunk = conn.execute("SELECT ord FROM article_chunks WHERE article_id=%s AND lang='zh' ORDER BY ord LIMIT 1",
                             (article_id,)).fetchone()["ord"]
        quoted = client.get(f"/articles/{article_id}?chunk={chunk}").text
        assert "is-quoted" in quoted and 'class="quoted"' not in quoted


REVIEW_SQL = """
SELECT t.article_id, jsonb_array_length(t.provenance->'number_review') AS n
FROM article_translations t
WHERE jsonb_array_length(coalesce(t.provenance->'number_review', '[]'::jsonb)) > 0
ORDER BY n DESC, t.article_id
"""


def test_unverified_numbers_are_stored_listed_and_still_paired_on_the_page(conn, settings, monkeypatch):
    """规则认不出「20世纪90年代」：照样入库、出队列，回执和质检列出这一段，页面仍逐段对照。"""
    english = "In the 1990s, Microsoft dominated the personal computer."
    article_id = article(conn, settings, f"<p>{english}</p><p>Second paragraph 20 dollars.</p>")

    def decade(**kwargs):
        if kwargs["user"].get("verify"):
            if "1990s" in kwargs["user"]["source"]:
                return ChatJsonResult(
                    data={"evidence_id": kwargs["user"]["evidence_id"], "verdict": "uncertain", "claims": []},
                    model="synthetic-model", usage=None, latency_ms=0,
                )
            return model(**kwargs)
        result = model(**kwargs)
        for item in result.data["t"]:
            item["html"] = item["html"].replace(english, "20世纪90年代，微软主导了个人电脑。")
        return result

    monkeypatch.setattr(service, "chat_json", decade)
    result = service.translate_article(conn, article_id, data_dir=settings.data_dir)
    assert result.status == "translated" and result.review == 1
    row = snapshot(conn, article_id)
    assert row["complete"] and row["provenance"]["number_review"] == [0]
    assert [item["article_id"] for item in conn.execute(REVIEW_SQL).fetchall()] == [article_id]
    assert article_id not in service.list_pending_english(conn, limit=50)
    report = qa.check_article(conn, settings, article_id)
    assert report.correspondence == "mapped" and report.status == "warn"
    assert [issue.code for issue in report.issues] == ["numbers-unverified"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}").text
        assert page.count('class="bilingual-pair"') == 2
        assert "20世纪90年代" in page and "第二段20美元" in page


def test_rejected_attempt_preserves_good_translation_and_search_chunks(conn, settings, monkeypatch):
    body = BODY + "<p>See inline <code>rate = 42</code>.</p>"
    article_id = article(conn, settings, body)
    monkeypatch.setattr(service, "chat_json", model)
    service.translate_article(conn, article_id, data_dir=settings.data_dir)
    before = snapshot(conn, article_id)
    chunks = conn.execute("SELECT body FROM article_chunks WHERE article_id=%s AND lang='zh' ORDER BY ord",
                          (article_id,)).fetchall()

    def missing(**kwargs):
        if kwargs["user"].get("verify"):
            return model(**kwargs)
        result = model(**kwargs)
        for item in result.data["t"]:
            item["html"] = item["html"].replace("⟦0⟧", "")
        return result

    monkeypatch.setattr(service, "chat_json", missing)
    with pytest.raises(AlignmentError, match="unshield") as rejected:
        service.translate_article(conn, article_id, data_dir=settings.data_dir)
    conn.rollback()
    service.record_translation_failure(conn, article_id, rejected.value)
    assert snapshot(conn, article_id) == before
    assert conn.execute("SELECT body FROM article_chunks WHERE article_id=%s AND lang='zh' ORDER BY ord",
                        (article_id,)).fetchall() == chunks
    attempt = conn.execute("SELECT status,reason FROM translation_attempts WHERE article_id=%s",
                           (article_id,)).fetchone()
    assert attempt == {"status": "rejected", "reason": "unshield"}


def test_cleaner_drift_is_visible_without_requeueing_old_complete_translation(conn, settings, monkeypatch):
    raw = "# Synthetic audit\n- source: member-rss\n\n" + BODY
    article_id = article(conn, settings, raw)
    monkeypatch.setattr(service, "chat_json", model)
    with monkeypatch.context() as old:
        old.setattr(service, "clean_article", lambda _conn, _id, _source, body: body)
        service.translate_article(conn, article_id, data_dir=settings.data_dir)
    before = snapshot(conn, article_id)
    report = qa.check_article(conn, settings, article_id)
    assert report.status == "fail" and report.correspondence == "unmapped"
    assert report.correspondence_reason == "source-fingerprint"
    assert article_id not in service.list_pending_english(conn, source_id="stratechery", limit=100)
    assert snapshot(conn, article_id) == before


def test_historical_audit_is_read_only_and_plan_contains_no_article_text(conn, settings, monkeypatch, tmp_path, capsys):
    article_id = article(conn, settings)
    monkeypatch.setattr(service, "chat_json", model)
    service.translate_article(conn, article_id, data_dir=settings.data_dir)
    before = snapshot(conn, article_id)
    monkeypatch.setattr(audit.Settings, "from_env", staticmethod(lambda: settings))
    path = tmp_path / "private-plan.json"
    assert audit.main(["--article-id", str(article_id), "--dry-run", "--plan", str(path)]) == 0
    value = path.read_text()
    assert '"readOnly": true' in value and '"category": "mapped"' in value
    assert "First paragraph" not in value and "第一段" not in value
    assert snapshot(conn, article_id) == before
    assert "modelCalls" in capsys.readouterr().out


def test_legacy_default_provenance_does_not_change_completion_or_queue(conn, settings):
    article_id = article(conn, settings)
    assert article_id in service.list_pending_english(conn, limit=100, source_id="stratechery")
    row = conn.execute("SELECT content_hash FROM articles WHERE id=%s", (article_id,)).fetchone()
    conn.execute(
        "INSERT INTO article_translations(article_id,content_hash,body_html,body_text,complete)"
        " VALUES(%s,%s,%s,%s,true)", (article_id, row["content_hash"], BODY, BODY),
    )
    conn.commit()
    assert snapshot(conn, article_id)["provenance"] == {}
    assert article_id not in service.list_pending_english(conn, limit=100, source_id="stratechery")
    report = qa.check_article(conn, settings, article_id)
    assert report.correspondence == "legacy"


def test_source_change_while_model_is_running_does_not_write_a_stale_result(conn, settings, monkeypatch):
    article_id = article(conn, settings)
    other = None

    def changed(**kwargs):
        nonlocal other
        if other is None:
            from researchlib.db import connect

            other = connect(settings)
            other.execute("UPDATE articles SET content_hash='changed-in-test' WHERE id=%s", (article_id,))
            other.commit()
        return model(**kwargs)

    monkeypatch.setattr(service, "chat_json", changed)
    try:
        with pytest.raises(AlignmentError, match="source-changed-during-request"):
            service.translate_article(conn, article_id, data_dir=settings.data_dir)
        conn.rollback()
        assert conn.execute("SELECT count(*) AS n FROM article_translations WHERE article_id=%s",
                            (article_id,)).fetchone()["n"] == 0
    finally:
        if other:
            other.close()

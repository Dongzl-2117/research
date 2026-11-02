"""独立测试库中核对真实写入、失败保留、迁移兼容和读取告警；模型全部替身。"""

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate import service
from researchlib.translate.integrity import PROCESSING_VERSION
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.qa import check_article
from researchlib.web.app import create_app

BODY = "<p>Alpha earned 10 dollars.</p><p>Beta spent 20 dollars.</p><p>Gamma held 30 dollars.</p>"
TRANSLATIONS = ["<p>甲赚了10美元。</p>", "<p>乙花了20美元。</p>", "<p>丙持有30美元。</p>"]


def seed(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "gudujinhuati")
    store_documents(conn, source=source,
                    docs=[ParsedDoc(external_id="synthetic:integrity", title="Synthetic report", published_at=None,
                                    body_md=BODY, raw_meta={"format": "html", "language": "en"}, image_refs=[])],
                    data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    return conn.execute("SELECT id FROM articles WHERE external_id = 'synthetic:integrity'").fetchone()["id"]


def model(**kwargs):
    parts = kwargs["user"]["segments"]
    texts = TRANSLATIONS if len(parts) == 3 else ["合成报告"]
    return ChatJsonResult(data={"ids": kwargs["user"]["segmentIds"], "t": texts},
                          model="synthetic", usage=None, latency_ms=0)


def test_new_writes_record_order_and_bad_retries_preserve_good_row(conn, settings, monkeypatch):
    import pytest

    article_id = seed(conn, settings)
    monkeypatch.setattr(service, "chat_json", model)
    result = service.translate_article(conn, article_id, data_dir=settings.data_dir)
    assert result.status == "translated" and result.segments == 3
    original = conn.execute("SELECT * FROM article_translations WHERE article_id = %s", (article_id,)).fetchone()
    chunks = conn.execute("SELECT * FROM article_chunks WHERE article_id = %s ORDER BY ord", (article_id,)).fetchall()
    assert original["provenance"]["version"] == PROCESSING_VERSION
    assert len(original["provenance"]["segments"]) == 3
    assert check_article(conn, settings, article_id).mapping_status == "recorded"

    def bad(**kwargs):
        parts = kwargs["user"]["segments"]
        return ChatJsonResult(data={"ids": kwargs["user"]["segmentIds"], "t": ["<p>数字丢了。</p>"] * len(parts)},
                              model="synthetic", usage=None, latency_ms=0)

    monkeypatch.setattr(service, "chat_json", bad)
    with pytest.raises(RuntimeError, match="numbers"):
        service.translate_article(conn, article_id, data_dir=settings.data_dir)
    current = conn.execute("SELECT * FROM article_translations WHERE article_id = %s", (article_id,)).fetchone()
    current_chunks = conn.execute(
        "SELECT * FROM article_chunks WHERE article_id = %s ORDER BY ord", (article_id,),
    ).fetchall()
    assert current == original and current_chunks == chunks

    def bad_title(**kwargs):
        result = model(**kwargs)
        if len(kwargs["user"]["segments"]) == 1:
            result.data["t"] = [""]
        return result

    monkeypatch.setattr(service, "chat_json", bad_title)
    result = service.translate_article(conn, article_id, data_dir=settings.data_dir)
    assert result.status == "partial" and "保留已有完整译文" in result.reason
    current = conn.execute("SELECT * FROM article_translations WHERE article_id = %s", (article_id,)).fetchone()
    assert current == original


def test_legacy_complete_stays_out_of_queue_and_warns_in_real_route(conn, settings, monkeypatch):
    article_id = seed(conn, settings)
    digest = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (article_id,)).fetchone()["content_hash"]
    conn.execute("""INSERT INTO article_translations
                    (article_id, content_hash, title_zh, body_html, body_text, complete, segments)
                    VALUES (%s, %s, '合成报告', %s, '合成译文', true, 3)""",
                 (article_id, digest, "".join(TRANSLATIONS)))
    conn.commit()
    assert article_id not in service.list_pending_english(conn, limit=20)
    report = check_article(conn, settings, article_id)
    assert report.status == "warn" and report.mapping_status == "legacy-unverified"
    monkeypatch.setattr("researchlib.web.routes.schedule_digests", lambda *args: None)
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/articles/{article_id}")
    assert response.status_code == 200
    assert "尚未核验逐段对应" in response.text
    row = conn.execute(
        "SELECT complete, provenance FROM article_translations WHERE article_id = %s", (article_id,),
    ).fetchone()
    assert row == {"complete": True, "provenance": None}


def test_cleaning_drift_is_reported_without_automatic_paid_rerun(conn, settings, monkeypatch):
    article_id = seed(conn, settings)
    monkeypatch.setattr(service, "chat_json", model)
    service.translate_article(conn, article_id, data_dir=settings.data_dir)
    monkeypatch.setattr(
        service, "_cleaned_body", lambda conn, article: BODY.replace("<p>Beta spent 20 dollars.</p>", ""),
    )
    report = check_article(conn, settings, article_id)
    assert report.mapping_status == "canonical-input-changed"
    assert report.status == "fail" and report.display_notice
    assert article_id not in service.list_pending_english(conn, limit=20)

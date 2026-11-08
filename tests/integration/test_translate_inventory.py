"""中英对照盘点：只读，各类分得对，和待译队列用同一套冷却规则。模型为固定替身。"""

from dataclasses import replace
from datetime import UTC, datetime

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate import inventory, service
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.provenance import ALIGNMENT_VERSION

BODY = "<p>First paragraph 10 dollars about {name}.</p><p>Second paragraph 20 dollars.</p>"
ZH = "<p>第一段10美元。</p><p>第二段20美元。</p>"


def _article(conn, settings, external_id, *, language="en", body=BODY):
    # 正文各不相同：一样的正文会被认成同一篇的重复，只留主版本。
    source = next(s for s in load_sources(settings.sources_path) if s.id == "stratechery")
    source = replace(source, min_chars=0)
    store_documents(conn, source=source, docs=[ParsedDoc(
        external_id, f"Synthetic {external_id}", datetime(2026, 1, 1, tzinfo=UTC), body.format(name=external_id),
        {"format": "markdown", "language": language}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    return conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()["id"]


def _model(**kwargs):
    user = kwargs["user"]
    values = []
    for identity, source_hash, html in zip(user["segment_ids"], user["source_hashes"], user["segments"], strict=True):
        translated = html.replace("First paragraph ", "第一段").replace("Second paragraph ", "第二段")
        translated = translated.replace(" dollars", "美元").replace("Synthetic ", "合成")
        values.append({"id": identity, "source_hash": source_hash, "html": translated})
    return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)


def _old_translation(conn, article_id, *, complete=True):
    conn.execute(
        "INSERT INTO article_translations"
        " (article_id, content_hash, title_zh, body_html, body_text, complete, segments)"
        " SELECT id, content_hash, '合成', %s, '第一段10美元。第二段20美元。', %s, 2 FROM articles WHERE id = %s",
        (ZH, complete, article_id),
    )


def _attempt(conn, article_id, status, reason):
    conn.execute(
        "INSERT INTO translation_attempts (article_id, status, reason, content_hash, alignment_version)"
        " SELECT id, %s, %s, content_hash, %s FROM articles WHERE id = %s",
        (status, reason, ALIGNMENT_VERSION, article_id),
    )


def _counts(conn):
    return conn.execute(
        "SELECT (SELECT count(*) FROM article_translations) AS t, (SELECT count(*) FROM translation_attempts) AS a"
    ).fetchone()


def test_inventory_sorts_every_english_article_and_writes_nothing(conn, settings, monkeypatch):
    done = _article(conn, settings, "done")
    monkeypatch.setattr(service, "chat_json", _model)
    assert service.translate_article(conn, done, data_dir=settings.data_dir).status == "translated"
    legacy = _article(conn, settings, "legacy")
    _old_translation(conn, legacy)
    fresh = _article(conn, settings, "fresh")
    stopped = _article(conn, settings, "stopped")
    _old_translation(conn, stopped, complete=False)
    _attempt(conn, stopped, "rejected", "untranslated:links")
    _attempt(conn, stopped, "rejected", "untranslated:links")
    cooling = _article(conn, settings, "cooling")
    _attempt(conn, cooling, "failed", "request-failed")
    _article(conn, settings, "chinese", language="zh", body="<p>这一篇本来就是中文，不进队列。</p>")
    conn.commit()
    before = dict(_counts(conn))

    report = inventory.collect(conn, settings)

    ids = report["ids"]
    assert report["english"] == 5
    assert ids["receipt"] == [done] and ids["legacy"] == [legacy] and ids["old-receipt"] == []
    assert sorted(ids["none"]) == sorted([fresh, cooling]) and ids["partial"] == [stopped]
    # 和待译队列同一套：拒满两次的停住，刚断线的在冷却，别的现在就能译。
    assert ids["pending-now"] == [fresh] and ids["stopped"] == [stopped] and ids["cooldown"] == [cooling]
    assert report["lastReasons"] == {"untranslated:links": 1, "request-failed": 1}
    assert report["display"]["mapped"] == [done]
    assert report["display"]["legacy-structural"] == [legacy]
    assert report["readOnly"] and report["modelCalls"] == 0
    assert dict(_counts(conn)) == before

    lines = inventory.summary(report)
    assert lines[0] == "英文文章 5 篇（stratechery 5）"
    assert "  从没译过 2" in lines
    assert "  这三类里：现在就能进队列 1，冷却中 1，同一版本拒满 2 次停住 1" in lines
    assert lines[-1] == "已译完的 2 篇页面：结构对得上、没核验 1，逐段对上 1"


def test_inventory_can_skip_rendering_pages(conn, settings, monkeypatch):
    legacy = _article(conn, settings, "legacy")
    _old_translation(conn, legacy)
    conn.commit()
    monkeypatch.setattr(inventory, "_display_category", lambda *args: (_ for _ in ()).throw(AssertionError))
    report = inventory.collect(conn, settings, pages=False)
    assert report["ids"]["legacy"] == [legacy] and report["display"] == {}

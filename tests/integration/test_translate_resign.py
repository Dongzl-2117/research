"""不调模型重签（审核 T1、T3）和只有图的帖子不再占待译名额（审核 T7）。模型为固定替身。"""

import json

from test_translate_inventory import _article, _model

from researchlib.health import notices, read_status
from researchlib.translate import resign, service
from researchlib.translate.provenance import receipt, source_manifest


class _Borrowed:
    """命令行用完会关连接；测试的连接还要接着用，关的时候只回滚。"""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self._conn.rollback()


def _no_model(**_kwargs):
    raise AssertionError("重签不许调模型")


def _break_entities(conn, settings, article_id):
    """做出 11-11 那批的样子：译文和回执里 AT&T 存成「AT&T;」，回执照样签得上。"""
    row = conn.execute("SELECT * FROM articles WHERE id = %s", (article_id,)).fetchone()
    source, _hash = service._render_original(conn, row, settings.data_dir)
    manifest = source_manifest(source)
    stored = conn.execute("SELECT body_html FROM article_translations WHERE article_id = %s",
                          (article_id,)).fetchone()["body_html"]
    broken = stored.replace("第一段", "AT&T;第一段")
    proof = receipt(manifest, broken)
    conn.execute("UPDATE article_translations SET body_html = %s, provenance = %s WHERE article_id = %s",
                 (broken, json.dumps(proof), article_id))
    conn.commit()


def test_resign_repairs_stored_entities_without_the_model(conn, settings, monkeypatch):
    body = "<p>First paragraph 10 dollars about {name} and AT&amp;T.</p><p>Second paragraph 20 dollars.</p>"
    article = _article(conn, settings, "att", body=body)
    monkeypatch.setattr(service, "chat_json", _model)
    assert service.translate_article(conn, article, data_dir=settings.data_dir).status == "translated"
    _break_entities(conn, settings, article)
    monkeypatch.setattr(service, "chat_json", _no_model)
    assert resign.why(conn, article, settings) == "entities"
    conn.rollback()
    found = resign.run(conn, settings, say=lambda _text: None)
    assert found["resigned"] == [article] and found["needs-model"] == []
    row = conn.execute("SELECT body_html, provenance FROM article_translations WHERE article_id = %s",
                       (article,)).fetchone()
    assert "&T;" not in row["body_html"] and "AT&amp;T" in row["body_html"]
    assert resign.why(conn, article, settings) == ""
    chunks = conn.execute("SELECT string_agg(body, ' ') AS text FROM article_chunks"
                          " WHERE article_id = %s AND lang = 'zh'", (article,)).fetchone()["text"]
    assert "AT&T" in chunks and "AT&T;" not in chunks
    conn.rollback()
    # 再跑一遍什么都不用做。
    again = resign.run(conn, settings, say=lambda _text: None)
    assert again["resigned"] == [] and again["needs-model"] == []


def test_resign_lists_articles_that_need_the_model_and_status_reports_them(conn, settings, monkeypatch):
    article = _article(conn, settings, "changed")
    monkeypatch.setattr(service, "chat_json", _model)
    service.translate_article(conn, article, data_dir=settings.data_dir)
    # 回执里有段被当成标签吞掉了字：不能复用，要调模型，重签不写。
    row = conn.execute("SELECT * FROM articles WHERE id = %s", (article,)).fetchone()
    source, _hash = service._render_original(conn, row, settings.data_dir)
    stored = conn.execute("SELECT body_html FROM article_translations WHERE article_id = %s",
                          (article,)).fetchone()["body_html"]
    swallowed = stored.replace("第二段", "第二段<cost holds and margin>", 1)
    conn.execute("UPDATE article_translations SET body_html = %s, provenance = %s WHERE article_id = %s",
                 (swallowed, json.dumps(receipt(source_manifest(source), swallowed)), article))
    conn.commit()
    monkeypatch.setattr(service, "chat_json", _no_model)
    monkeypatch.setattr(resign.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(resign, "connect", lambda _settings: _Borrowed(conn))
    assert resign.main([]) == 0
    status = read_status(settings.data_dir, resign.TASK)
    assert status["ok"] is False and status["needsModel"] == [article]
    assert any("中英对照重签失败" in line for line in notices(settings.data_dir, "Asia/Shanghai"))
    unchanged = conn.execute("SELECT body_html FROM article_translations WHERE article_id = %s",
                             (article,)).fetchone()["body_html"]
    assert unchanged == swallowed


def test_image_only_posts_stop_taking_queue_slots(conn, settings, monkeypatch):
    only_images = _article(conn, settings, "pictures", body="![](https://example.com/{name}.png)")
    text = _article(conn, settings, "words")
    assert set(service.list_pending_english(conn, limit=10)) >= {only_images, text}
    monkeypatch.setattr(service, "chat_json", _no_model)
    result = service.translate_article(conn, only_images, data_dir=settings.data_dir)
    assert result.status == "skipped" and result.reason == "no translatable text"
    pending = service.list_pending_english(conn, limit=10)
    assert only_images not in pending and text in pending
    # 再跑一次不重复记。
    service.translate_article(conn, only_images, data_dir=settings.data_dir)
    n = conn.execute("SELECT count(*) AS n FROM translation_attempts WHERE article_id = %s AND status = 'no-text'",
                     (only_images,)).fetchone()["n"]
    assert n == 1


def test_boilerplate_image_comparison_runs_read_only(conn, settings):
    """审核 C7 的只读对照在真库上跑得通：没有重复图时什么都不列。"""
    from researchlib.boilerplate_images import compare

    _article(conn, settings, "plain")
    assert compare(conn, "stratechery") == []

"""research-translate --workers：每篇一把会话级 advisory lock，几个 worker、几个进程不会同时译同一篇。

模型为固定替身。
"""

from dataclasses import replace
from datetime import UTC, datetime

from researchlib.db import connect
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate import cli, service
from researchlib.translate.llm import ChatJsonResult


def _article(conn, settings, external_id):
    source = next(s for s in load_sources(settings.sources_path) if s.id == "stratechery")
    source = replace(source, min_chars=0)
    body = f"<p>First paragraph 10 dollars about {external_id}.</p><p>Second paragraph 20 dollars.</p>"
    store_documents(conn, source=source, docs=[ParsedDoc(
        external_id, f"Synthetic {external_id}", datetime(2026, 1, 1, tzinfo=UTC), body,
        {"format": "markdown", "language": "en"}, [],
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


def test_the_lock_is_per_article_and_survives_a_rollback(conn, settings):
    other = connect(settings)
    try:
        assert cli._try_lock(conn, 101)
        # run() 失败时先回滚再记失败、放锁：会话级的锁不跟着事务走。
        conn.rollback()
        assert not cli._try_lock(other, 101)
        assert cli._try_lock(other, 102)
        cli._unlock(conn, 101)
        assert cli._try_lock(other, 101)
        cli._unlock(other, 101)
        cli._unlock(other, 102)
        held = conn.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'").fetchone()["n"]
        assert held == 0
    finally:
        other.close()


def test_two_workers_translate_the_queue_with_their_own_connections(conn, settings, monkeypatch, capsys):
    ids = [_article(conn, settings, f"worker-{n}") for n in range(4)]
    monkeypatch.setattr(service, "chat_json", _model)
    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "llm_provider", lambda: "deepseek")
    monkeypatch.setattr(cli, "deepseek_api_key", lambda: "sk-test")

    assert cli.main(["--workers", "2", "--limit", "4"]) == 0

    out = capsys.readouterr().out
    assert sorted(int(line.split()[0][1:]) for line in out.splitlines() if " translated " in line) == sorted(ids)
    rows = conn.execute(
        "SELECT t.article_id, t.complete, count(c.*) AS chunks FROM article_translations t"
        " LEFT JOIN article_chunks c ON c.article_id = t.article_id AND c.lang = 'zh'"
        " GROUP BY t.article_id, t.complete ORDER BY t.article_id"
    ).fetchall()
    assert [row["article_id"] for row in rows] == sorted(ids)
    assert all(row["complete"] and row["chunks"] > 0 for row in rows)
    assert conn.execute("SELECT count(*) AS n FROM translation_attempts").fetchone()["n"] == 0
    # 队列空了，再跑一次什么也不译。
    assert cli.main(["--workers", "2"]) == 0
    assert "没有待译英文文章。" in capsys.readouterr().out

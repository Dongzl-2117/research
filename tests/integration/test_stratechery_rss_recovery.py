"""会员源、官网和模型都用本地样例，抓取写盘、导入和待译查询走正常入口。"""

from xml.sax.saxutils import escape

from researchlib import stratechery_crawl as rss
from researchlib.ingest import cli as ingest_cli
from researchlib.translate.service import list_pending_english

LONG = "".join(
    f"<p>Section {i} discusses revenue recognition and the cost of shipping hardware. "
    f"The terminal cash flow estimate for scenario {i} assumes recurring sales.</p>"
    for i in range(120)
)


def _feed(*entries):
    ns = "http://purl.org/rss/1.0/modules/content/"
    items = []
    for slug, title, body, when in entries:
        items.append(
            f"<item><title>{escape(title)}</title><link>https://stratechery.com/2026/{slug}/</link>"
            f"<pubDate>{when}</pubDate><content:encoded><![CDATA[{body}]]></content:encoded></item>"
        )
    return (f'<rss xmlns:content="{ns}"><channel>' + "".join(items) + "</channel></rss>").encode()


def _setup(settings, monkeypatch, feed):
    monkeypatch.setenv("STRATECHERY_RSS_URL", "https://example.invalid/test-feed")
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SOURCES_FILE", str(settings.sources_path))
    monkeypatch.setattr(rss, "fetch_feed", lambda url: feed[0])


def test_full_feed_unlocks_same_short_article_and_only_queues_its_source(conn, settings, monkeypatch):
    date = "Mon, 02 Nov 2026 11:00:00 +0000"
    feed = [_feed(("cash-flow", "Cash Flow", "<p>Subscriber preview.</p>", date))]
    _setup(settings, monkeypatch, feed)
    assert rss.main([]) == 0
    before = conn.execute("SELECT id, raw_meta FROM articles WHERE source_id='stratechery'").fetchone()
    assert before["raw_meta"]["hidden"] == "short_post"
    assert list_pending_english(conn, source_id="stratechery") == []
    feed[0] = _feed(("cash-flow", "Cash Flow", LONG, date))
    assert rss.main([]) == 0
    after = conn.execute("SELECT id, raw_meta, body_text FROM articles WHERE source_id='stratechery'").fetchone()
    assert after["id"] == before["id"] and not after["raw_meta"].get("hidden")
    assert "scenario 119" in after["body_text"]
    assert list_pending_english(conn, source_id="stratechery") == [after["id"]]
    assert list_pending_english(conn, source_id="fangwei-mp") == []
    feed[0] = _feed(("cash-flow", "Cash Flow", "<p>Preview again.</p>", date))
    assert rss.main([]) == 0
    saved = conn.execute("SELECT body_text FROM articles WHERE id=%s", (after["id"],)).fetchone()
    assert saved["body_text"] == after["body_text"]


def test_archived_article_retries_after_failed_import_even_after_it_leaves_feed(conn, settings, monkeypatch):
    date = "Mon, 02 Nov 2026 11:00:00 +0000"
    feed = [_feed(("first", "First", LONG, date))]
    _setup(settings, monkeypatch, feed)
    with monkeypatch.context() as patch:
        patch.setattr(ingest_cli, "run_markdown_ingest", lambda *args, **kwargs: 1)
        assert rss.main([]) == 1
    assert (settings.data_dir / "inbox/stratechery/2026/first.md").is_file()
    feed[0] = _feed(("second", "Second", "<p>Latest.</p>", date))
    assert rss.main([]) == 0
    ids = [r["external_id"] for r in conn.execute("SELECT external_id FROM articles ORDER BY external_id")]
    assert ids == ["2026/first", "2026/second"]
    assert rss.main([]) == 0
    assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 2


def test_recovery_uses_local_full_body_even_when_current_feed_reverts_to_preview(conn, settings, monkeypatch):
    date = "Mon, 02 Nov 2026 11:00:00 +0000"
    feed = [_feed(("original", "Original", LONG, date))]
    _setup(settings, monkeypatch, feed)
    assert rss.main(["--no-import"]) == 0
    archived = settings.data_dir / "inbox/stratechery/2026"
    # 格式相似但链接和 id 指向另一个归档，不能进入本来源。
    (archived / "another-source.md").write_text((archived / "original.md").read_text())
    feed[0] = _feed(("original", "Original", "<p>Current preview only.</p>", date))
    assert rss.main([]) == 0
    found = conn.execute("SELECT external_id, body_text, raw_meta FROM articles").fetchall()
    assert len(found) == 1 and found[0]["external_id"] == "2026/original"
    assert "scenario 119" in found[0]["body_text"] and not found[0]["raw_meta"].get("hidden")

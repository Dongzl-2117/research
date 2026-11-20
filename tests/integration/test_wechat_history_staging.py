"""历史正文待确认时，定时抓取只补导日常归档。"""

from researchlib import llm_cost, wechat_crawl
from researchlib.ingest import cli, wechat


def test_history_waits_for_explicit_import_while_daily_archive_retries(conn, settings, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("SOURCES_FILE", str(settings.sources_path))
    monkeypatch.setenv("DAJIALA_KEY", "test-key")
    source = ["--source", "latepost"]

    def post(char, title, timestamp):
        sn = char * 32
        return {"url": f"https://mp.weixin.qq.com/s?sn={sn}", "sn": sn, "title": title, "post_time": timestamp}

    historical = post("a", "待确认的历史资料", 1690000000)
    daily = post("b", "当日的普通资料", 1790000000)
    listing = [historical]
    calls, fetched = [], []

    class Client:
        cost = 0.0

        def history(self, ghid):
            calls.append(ghid)
            return wechat_crawl.parse_posts(listing), {"is_end": 1, "nickname": "晚点LatePost"}

    def fetch(url):
        fetched.append(url)
        return f'<div id="js_content" class="rich_media_content"><p>测试归档正文 {url[-1]}。</p></div>'

    monkeypatch.setattr(wechat_crawl, "Dajiala", lambda *args: Client())
    monkeypatch.setattr(wechat_crawl, "fetch_page", fetch)

    def titles():
        return {r["title"] for r in conn.execute("SELECT title FROM articles WHERE source_id='latepost'")}

    assert wechat_crawl.main([*source, "--list-only"]) == 0
    assert wechat_crawl.main([*source, "--backfill", "--max-pages", "0", "--no-import"]) == 0
    assert not titles() and len(calls) == 1
    # 日常新稿已归档，导入中断。下一次列表里只有以前暂存的历史稿，新稿也滑出了列表。
    listing[:] = [daily]
    with monkeypatch.context() as patch:
        patch.setattr(cli, "run_markdown_ingest", lambda *args, **kwargs: 1)
        assert wechat_crawl.main(source) == 1
    listing[:] = [historical]
    assert wechat_crawl.main(source) == 0
    assert titles() == {daily["title"]}
    assert len(fetched) == 2

    # 确认前可用暂存的实文字数估费。
    archive = settings.data_dir / "backfill" / "latepost"
    listing_path, _state = wechat_crawl.history_paths(settings.data_dir / "inbox" / "latepost")
    specs, _skipped = llm_cost.specs_from_listing(listing_path, chars_by_sn=llm_cost.archive_chars(archive))
    assert len(specs) == 1 and specs[0].chars > 0 and not specs[0].assumed
    # 同意之后按文档手动导入，再按历史回填命令重跑；游标和归档都能复用。
    assert wechat.main([*source, "--input", str(archive), "--origin", "crawl"]) == 0
    assert wechat_crawl.main([*source, "--backfill", "--max-pages", "0"]) == 0
    assert titles() == {daily["title"], historical["title"]}
    assert conn.execute("SELECT count(*) AS n FROM articles WHERE source_id='latepost'").fetchone()["n"] == 2
    assert len(calls) == 3 and len(fetched) == 2

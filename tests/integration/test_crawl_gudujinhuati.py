"""孤独进化体定时抓取：research-crawl-wechat 写归档、按 crawl 导入，再跑一次不重复取。不连网。"""

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from researchlib import wechat_crawl
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

SN_NEW = "0123456789abcdef0123456789abcdef"
SN_OLD = "80cf736553b7449cba800ae0f9bc427e"
PAGE = """<html><body><div class="rich_media_content" id="js_content">
<section><p>德合大道无为的新一封信，讲开市客和沃尔玛的会员费。</p>
<p><img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/abc/640?wx_fmt=jpeg"></p>
<p>会员费收入占利润的大头，商品只赚很薄的毛利。</p></section>
</div></body></html>"""


class FakeClient:
    def __init__(self, posts):
        self.posts = posts
        self.cost = 0.0
        self.history_calls = 0

    def history(self, ghid):
        assert ghid == "gh_1f869b9dc034"
        self.history_calls += 1
        self.cost += 0.14
        return wechat_crawl.parse_posts(self.posts), {"remain_money": 20.0}

    def article(self, url):
        raise AssertionError("原文页有正文，不该再花钱取极致了的正文")


def _post(sn, title, when):
    return {
        "url": f"http://mp.weixin.qq.com/s?__biz=MzU5MDUzNTcxMA==&mid=1&idx=1&sn={sn}#rd",
        "title": title,
        "post_time": int(when.timestamp()),
        "sn": sn,
    }


def _env(monkeypatch, settings):
    for key, value in {
        "DATABASE_URL": settings.database_url,
        "DATA_DIR": str(settings.data_dir),
        "SOURCES_FILE": str(settings.sources_path),
        "PUBLIC_BASE_URL": settings.public_base_url,
        "DISPLAY_TIMEZONE": "Asia/Shanghai",
        "DAJIALA_KEY": "test-key",
    }.items():
        monkeypatch.setenv(key, value)


def test_crawl_imports_new_posts_once(conn, settings, monkeypatch, capsys):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "gudujinhuati")
    # 以前从归档导进来的一篇：归档头里有 sn。
    store_documents(
        conn,
        source=source,
        docs=[
            ParsedDoc(
                "md/2026/old.md",
                "德合大道无为——2026年中期致合伙人信",
                datetime(2026, 8, 28, 9, 0, tzinfo=SHANGHAI),
                f"# 德合大道无为——2026年中期致合伙人信\n\n- 作者：孤独进化体\n- sn：`{SN_OLD}`\n\n正文。\n",
                {"format": "markdown"},
                [],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    client = FakeClient(
        [
            _post(SN_NEW, "开市客的会员费", datetime(2026, 10, 12, 1, 30, tzinfo=UTC)),
            _post(SN_OLD, "德合大道无为——2026年中期致合伙人信", datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
        ]
    )
    _env(monkeypatch, settings)
    monkeypatch.setattr(wechat_crawl, "Dajiala", lambda key, base: client)
    pages = []
    monkeypatch.setattr(wechat_crawl, "fetch_page", lambda url: pages.append(url) or PAGE)

    assert wechat_crawl.main(["--source", "gudujinhuati"]) == 0
    out = capsys.readouterr().out
    assert "已在库里: 1" in out and "新写入归档: 1" in out and "写入: 新增 1" in out
    assert len(pages) == 1

    row = conn.execute(
        "SELECT id, title, published_at, external_id, raw_meta FROM articles WHERE source_id = 'gudujinhuati'"
        " AND body_md LIKE %s",
        (f"%{SN_NEW}%",),
    ).fetchone()
    assert row["title"] == "开市客的会员费"
    assert row["published_at"] == datetime(2026, 10, 12, 9, 30, tzinfo=SHANGHAI)
    assert row["raw_meta"]["time_known"] is True
    assert row["external_id"] == f"md/2026/2026-10-12-{SN_NEW}.md"
    state = conn.execute("SELECT value FROM app_state WHERE key = 'import:gudujinhuati'").fetchone()
    assert json.loads(state["value"])["origin"] == "crawl"

    with TestClient(create_app(settings)) as web:
        html = web.get(f"/articles/{row['id']}").text
    prose = html[html.index('<div class="prose">') :].split("</article>", 1)[0]
    # 归档头（作者、时间、sn、原链接）不显示；图床的图等 research-fetch-images 下载。
    assert "<h1>开市客的会员费</h1>" in html
    assert "会员费收入占利润的大头" in prose and "外部图片已省略" in prose
    assert "作者：" not in prose and SN_NEW not in prose

    # 第二天再跑：库里和归档里都有了，不再取原文页，导入也不变。
    assert wechat_crawl.main(["--source", "gudujinhuati"]) == 0
    out = capsys.readouterr().out
    assert "已在库里: 2" in out and "新写入归档: 0" in out and "写入: 新增 0" in out
    assert len(pages) == 1
    count = conn.execute("SELECT count(*) AS n FROM articles WHERE source_id = 'gudujinhuati'").fetchone()["n"]
    assert count == 2


def test_crawl_refuses_sources_without_ghid(settings, monkeypatch, capsys):
    _env(monkeypatch, settings)
    assert wechat_crawl.main(["--source", "fangwei-mp"]) == 2
    assert "没写 ghid" in capsys.readouterr().err
    monkeypatch.delenv("DAJIALA_KEY")
    assert wechat_crawl.main(["--source", "gudujinhuati"]) == 2
    assert "缺少 DAJIALA_KEY" in capsys.readouterr().err

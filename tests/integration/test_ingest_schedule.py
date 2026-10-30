"""定时抓取导入的来源停更明显超过平时间隔时，首页提示。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app


def test_home_shows_a_notice_for_a_stalled_source(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    start = datetime.now(UTC) - timedelta(days=60)
    docs = [
        ParsedDoc(
            external_id=f"mp/{index}",
            title=f"第 {index} 篇",
            published_at=start + timedelta(days=2 * index),
            body_md=f"第 {index} 篇写的是腾讯第 {index} 个季度的广告收入和视频号的库存，和别的篇不一样。",
            raw_meta={},
            image_refs=[],
        )
        for index in range(12)
    ]

    def store(origin):
        store_documents(conn, source=sources["fangwei-mp"], docs=docs, data_dir=settings.data_dir,
                        archive_files=len(docs), images_in_archive=0, errors=[], origin=origin)

    # 手动导入的归档本来就是旧的：不提示。
    store("archive")
    with TestClient(create_app(settings)) as client:
        assert "没有新文章" not in client.get("/").text
    # 定时抓取刚导过，但最近一篇已经很久了：提示。
    store("crawl")
    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        assert "方伟公众号 37 天没有新文章" in home or "方伟公众号 38 天没有新文章" in home
        assert "最近一次导入" in home
    with TestClient(create_app(replace(settings, stale_banner_enabled=False))) as client:
        assert "没有新文章" not in client.get("/").text

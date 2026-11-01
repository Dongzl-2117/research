"""Stratechery 指向别篇的相对 .md 链接：文章页连到站内那一篇，.md 接口给站内完整地址。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _doc(external_id: str, title: str, body: str, day: int) -> ParsedDoc:
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2016, 6, day, 8, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={"language": "en", "parent": external_id.rsplit("/", 1)[0]},
        image_refs=[],
    )


def test_cross_links_reach_the_archived_post(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    store_documents(
        conn,
        source=sources["stratechery"],
        docs=[
            _doc("2015/a-framework", "A Framework", "The framework itself is described here at some length.", 1),
            _doc(
                "2016/a-follow-up",
                "A Follow-Up",
                "This builds on [the framework](../2015/a-framework.md) and on [a missing post](../2014/gone.md).",
                2,
            ),
        ],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    ids = {row["external_id"]: row["id"] for row in conn.execute("SELECT id, external_id FROM articles").fetchall()}
    target, follow_up = ids["2015/a-framework"], ids["2016/a-follow-up"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{follow_up}").text
        md = client.get(f"/api/v1/articles/{follow_up}.md").text
    assert f'href="/articles/{target}"' in page
    assert page.count("未收录") == 1
    assert f"[the framework]({settings.public_base_url}/articles/{target})" in md
    assert "a missing post" in md and "../2014/gone.md" not in md

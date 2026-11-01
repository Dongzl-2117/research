"""Stratechery 网页导出归档头里的「- rss_link: …」是导出脚本用的个人订阅地址：文章页、检索、接口、MCP 都不给。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 形状照 2026 年的网页导出：标题下面两行归档头，再是一行导语。地址是假的。
BODY = """---
id: "2026/widget-tv"
title: "Widget TV"
url: "https://stratechery.example/2026/widget-tv/"
---

# Widget TV

- source: member-rss
- rss_link: https://stratechery.example/?access_token=example-secret

The new Widget TV is official, and the question is how big the market for it really is.

Widget Corp says the device will ship next month at a price that undercuts every rival.
"""


def test_feed_address_stays_out_of_every_output(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "stratechery")
    doc = ParsedDoc(
        "2026/widget-tv", "Widget TV", datetime(2026, 10, 14, 8, 0, tzinfo=SHANGHAI), BODY, {"language": "en"}, []
    )
    store_documents(
        conn, source=source, docs=[doc], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[]
    )
    conn.commit()
    row = conn.execute("SELECT id, body_text FROM articles WHERE external_id = '2026/widget-tv'").fetchone()
    article_id = row["id"]
    chunks = conn.execute("SELECT body FROM article_chunks WHERE article_id = %s", (article_id,)).fetchall()
    assert "example-secret" not in row["body_text"] and chunks
    assert not any("example-secret" in chunk["body"] for chunk in chunks)

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}").text
        single = client.get(f"/api/v1/articles/{article_id}").json()
        markdown = client.get(f"/api/v1/articles/{article_id}.md").text
        call = client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_article", "arguments": {"id": article_id}},
            },
        ).json()
    mcp_text = call["result"]["content"][0]["text"]
    for output in (page, single["body_markdown"], markdown, mcp_text):
        assert "example-secret" not in output and "rss_link" not in output
        assert "The new Widget TV is official" in output
    # 别的归档头照旧是原文的一部分；原文自带的 frontmatter（原文地址）也还在 JSON 里。
    assert "- source: member-rss" in single["body_markdown"]
    assert 'url: "https://stratechery.example/2026/widget-tv/"' in single["body_markdown"]
    # 库里的原文不改。
    stored = conn.execute("SELECT body_md FROM articles WHERE id = %s", (article_id,)).fetchone()["body_md"]
    assert "example-secret" in stored

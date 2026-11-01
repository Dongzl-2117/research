"""Stratechery 导出的引文、列表项写了两遍：单看原文的页面和 .md 接口收成一份，中英对照照旧。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUOTE = "A note on these numbers comes first."
ITEM = "which keeps growing every single quarter."

BODY = (
    "The opening paragraph sets up the argument about three businesses, how each of them makes money, "
    "and why the next few years matter more than the last few for every one of them.\n"
    "\n"
    f"> {QUOTE}\n"
    "\n"
    f"{QUOTE}\n"
    "\n"
    f"- Search: The first point is about the ads business , {ITEM}\n"
    "\n"
    f"**Search:** The first point is about the [ads business](https://example.com/ads), {ITEM}\n"
    "\n"
    "The closing paragraph wraps it up."
)

ZH_HTML = (
    "<p>开头这一段讲三门生意各自怎么赚钱，以及为什么接下来几年比过去几年更要紧。</p>"
    "<blockquote><p>先说一下这些数字。</p></blockquote>"
    "<p>先说一下这些数字。</p>"
    "<ul><li><p>搜索：第一点是广告业务，每个季度都在长。</p></li></ul>"
    "<p><strong>搜索：</strong>第一点是广告业务，每个季度都在长。</p>"
    "<p>最后一段收尾。</p>"
)


def _store(conn, settings) -> int:
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    store_documents(
        conn,
        source=sources["stratechery"],
        docs=[
            ParsedDoc(
                external_id="2026/three-businesses",
                title="Three Businesses",
                published_at=datetime(2026, 5, 2, 8, 0, tzinfo=SHANGHAI),
                body_md=BODY,
                raw_meta={"language": "en"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    row = conn.execute("SELECT id FROM articles WHERE external_id = '2026/three-businesses'").fetchone()
    return row["id"]


def _prose(page: str) -> str:
    return page.split('<div class="prose', 1)[1].split("</div>", 1)[0]


def test_original_only_page_and_markdown_show_each_quote_once(conn, settings):
    article_id = _store(conn, settings)
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}").text
        md = client.get(f"/api/v1/articles/{article_id}.md").text
    prose = _prose(page)
    assert prose.count(QUOTE) == 1
    assert prose.count(ITEM) == 1
    item = prose.split("<li>", 1)[1].split("</li>", 1)[0]
    # 列表项里换成了带加粗和链接的那份。
    assert "<strong>Search:</strong>" in item and "https://example.com/ads" in item
    assert md.count(QUOTE) == 1
    assert "- **Search:** The first point is about the [ads business](https://example.com/ads)" in md
    # 库里的原文不改。
    stored = conn.execute("SELECT body_md FROM articles WHERE id = %s", (article_id,)).fetchone()["body_md"]
    assert stored.count(QUOTE) == 2


def test_bilingual_pairs_keep_the_export_as_is(conn, settings):
    article_id = _store(conn, settings)
    content_hash = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (article_id,)).fetchone()[
        "content_hash"
    ]
    conn.execute(
        """
        INSERT INTO article_translations (article_id, content_hash, title_zh, body_html, body_text, complete, segments)
        VALUES (%s, %s, '三门生意', %s, %s, true, 6)
        """,
        (article_id, content_hash, ZH_HTML, ZH_HTML),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        pairs = client.get(f"/articles/{article_id}").text
        original = client.get(f"/articles/{article_id}/original").text
    # 对照：译文是照着两份译的，原文照旧两份，一段对一段。过 80 字的列表项和副本由对照页
    # 自己收成一对（translate/pair.py），短的引文两份都还在。
    assert pairs.count('class="prose bilingual-en with-toolbar"') == 5
    assert pairs.count(QUOTE) == 2
    assert "先说一下这些数字。" in pairs
    # 点「原文」只看英文：收成一份。
    assert _prose(original).count(QUOTE) == 1
    assert _prose(original).count(ITEM) == 1

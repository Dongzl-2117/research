"""Stratechery 每日更新开头结尾的套话：检索不命中，单看原文的页面和 .md 接口不显示，对照照旧。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOTE = "A quick correction to yesterday's Update about the widget numbers."
SIGNOFF = (
    "The Stratechery Update is intended for a single recipient, but occasional forwarding is totally fine!\n"
    "If you would like to order multiple subscriptions for your team with a group discount\n"
    "(minimum 5), please contact me directly."
)

UPDATE = (
    "**Listen to this post:**\n\n"
    "Good morning,\n\n"
    f"{NOTE}\n\n"
    "On to the Update:\n\n"
    "Widget Corp had a strong quarter driven by its hardware business and a growing services line.\n\n"
    "This Update will be available as a podcast later today. To receive it in your podcast player, "
    "visit Stratechery.\n\n"
    f"{SIGNOFF}\n\n"
    "Thanks for being a subscriber, and have a great day!\n"
)
ARTICLE = (
    "Good morning,\n\n"
    "On to the Update:\n\n"
    "Gadget Inc. is moving its customers to annual subscriptions, which changes how it reports revenue.\n\n"
    f"{SIGNOFF}\n\n"
    "Thanks for being a subscriber, and have a great day!\n"
)


def _store(conn, settings) -> dict[str, int]:
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    store_documents(
        conn,
        source=sources["stratechery"],
        docs=[
            ParsedDoc(
                external_id=external_id,
                title=title,
                published_at=datetime(2026, 5, day, 8, 0, tzinfo=SHANGHAI),
                body_md=body,
                raw_meta={"language": "en"},
                image_refs=[],
            )
            for external_id, title, body, day in (
                ("2026/widget-earnings", "Widget Earnings", UPDATE, 2),
                ("2026/gadget-subscriptions", "Gadget Subscriptions", ARTICLE, 3),
            )
        ],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    rows = conn.execute("SELECT id, external_id FROM articles").fetchall()
    return {row["external_id"]: row["id"] for row in rows}


def _prose(page: str) -> str:
    return page.split('<div class="prose', 1)[1].split("</div>", 1)[0]


def test_search_and_original_page_skip_the_newsletter_lines(conn, settings):
    ids = _store(conn, settings)
    widget, gadget = ids["2026/widget-earnings"], ids["2026/gadget-subscriptions"]
    text = conn.execute("SELECT body_text FROM articles WHERE id = %s", (widget,)).fetchone()["body_text"]
    assert "single recipient" not in text and "Good morning" not in text and NOTE in text
    chunks = conn.execute("SELECT body FROM article_chunks WHERE article_id = %s", (widget,)).fetchall()
    assert chunks and not any("group discount" in row["body"] for row in chunks)
    with TestClient(create_app(settings)) as client:
        found = client.get("/api/v1/search", params={"q": "subscriptions"}).json()
        page = client.get(f"/articles/{widget}").text
        md = client.get(f"/api/v1/articles/{widget}.md").text
    # 只在转发须知里出现 subscriptions 的那篇不算命中，正文里讲订阅的那篇还在。
    hits = {item["id"] for item in found["items"]}
    assert gadget in hits and widget not in hits
    prose = _prose(page)
    for gone in ("Listen to this post", "Good morning", "On to the Update", "single recipient", "great day"):
        assert gone not in prose, gone
        assert gone not in md, gone
    assert NOTE in prose and NOTE in md
    assert "strong quarter" in prose
    # 库里的原文不改。
    stored = conn.execute("SELECT body_md FROM articles WHERE id = %s", (widget,)).fetchone()["body_md"]
    assert "single recipient" in stored


def test_bilingual_pairs_stay_aligned(conn, settings):
    ids = _store(conn, settings)
    widget = ids["2026/widget-earnings"]
    content_hash = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (widget,)).fetchone()["content_hash"]
    zh_html = (
        "<p><strong>收听本文：</strong></p>"
        "<p>早上好，</p>"
        "<p>更正一下昨天关于小部件数字的说法。</p>"
        "<p>下面进入正题：</p>"
        "<p>Widget 公司这个季度很强，靠硬件和越做越大的服务。</p>"
        "<p>本期稍后会出播客版。</p>"
        "<p>本期只发给一位收件人，偶尔转发没问题！<br>想给团队订多份<br>请直接联系我。</p>"
        "<p>感谢订阅，祝您愉快！</p>"
    )
    conn.execute(
        """
        INSERT INTO article_translations (article_id, content_hash, title_zh, body_html, body_text, complete, segments)
        VALUES (%s, %s, '小部件财报', %s, %s, true, 10)
        """,
        (widget, content_hash, zh_html, zh_html),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        pairs = client.get(f"/articles/{widget}").text
        original = client.get(f"/articles/{widget}/original").text
    # 对照按位置配：原文一段不少，正文那一段对着正文的译文。
    assert "Widget Corp had a strong quarter" in pairs and "Widget 公司这个季度很强" in pairs
    en_before = pairs.split("Widget Corp had a strong quarter", 1)[0]
    zh_before = pairs.split("Widget 公司这个季度很强", 1)[0]
    assert en_before.count("bilingual-en") == zh_before.count("bilingual-en")
    # 点「原文」只看英文：套话不显示。
    assert "single recipient" not in _prose(original) and NOTE in _prose(original)

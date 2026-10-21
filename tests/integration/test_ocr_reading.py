"""阅读页把截图里的字展开；摘要和观点的输入同样读到这些字。

样例按 14886 的形状：正文一句，判断在群聊截图里。公司提及表仍不看图片。
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib import digest as digest_mod
from researchlib import views as views_mod
from researchlib.chunks import replace_ocr_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ocr import refresh_article_ocr
from researchlib.render import image_anchor
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
CHAT = (
    "微信\n20:41\n张三\n瑞幸和古茗都算头部，但瑞幸同店还有压力\n"
    "李四\n古茗店效还行，瑞幸的折扣不能再深了\n张三\n先看到这里，不展开了\n"
)
JUDGMENT = "张三\n茅台批价这一周没动，渠道仍在观望\n李四\n短期不宜提价\n王五\n先看到这里\n"


def _png(path: Path, marker: bytes) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker)


def _store(conn, settings, source_id: str, external_id: str, title: str, body: str, images: list[Path]):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    doc = ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2026, 10, 18, 9, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={},
        image_refs=[(path.name, path) for path in images],
    )
    store_documents(
        conn,
        source=sources[source_id],
        docs=[doc],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=len(images),
        errors=[],
    )
    return conn.execute(
        "SELECT id, content_hash, source_id FROM articles WHERE external_id = %s AND source_id = %s",
        (external_id, source_id),
    ).fetchone()


def _remember(conn, article_id: int, ref: str, text: str) -> str:
    row = conn.execute(
        "SELECT content_hash FROM article_images WHERE article_id = %s AND original_ref = %s",
        (article_id, ref),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO image_texts (content_hash, engine, text) VALUES (%s, 'test', %s)
        ON CONFLICT (content_hash) DO UPDATE SET text = EXCLUDED.text
        """,
        (row["content_hash"], text),
    )
    return row["content_hash"]


def test_reader_shows_chat_text_and_collapses_the_screenshot(conn, settings, tmp_path):
    chat = tmp_path / "chat.png"
    logo = tmp_path / "logo.png"
    _png(chat, b"\x00" * 24)
    _png(logo, b"\x01" * 24)
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-14886",
        "图片",
        "瑞幸古茗是比较头部的。\n\n![](chat.png)\n\n![](logo.png)\n",
        [chat, logo],
    )
    chat_hash = _remember(conn, row["id"], "chat.png", CHAT)
    logo_hash = _remember(conn, row["id"], "logo.png", "logo")
    refresh_article_ocr(conn, row["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (row["id"],)).fetchone()
    assert "瑞幸和古茗都算头部" in saved["ocr_text"]
    assert "logo" not in saved["ocr_text"]

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}").text
    assert "瑞幸古茗是比较头部的" in page
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" in page
    assert "古茗店效还行，瑞幸的折扣不能再深了" in page
    assert "点击看原图" in page
    assert f'<figure class="ocr-shot" id="{image_anchor(chat_hash)}">' in page
    assert f'<img id="{image_anchor(chat_hash)}"' not in page
    assert page.index("瑞幸和古茗都算头部") < page.index("<details")
    assert f'<img id="{image_anchor(logo_hash)}"' in page
    assert page.count("点击看原图") == 1


def test_short_screenshot_post_is_read_by_views_and_digest(conn, settings, tmp_path, monkeypatch):
    image = tmp_path / "chat.png"
    _png(image, b"\x02" * 24)
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-view",
        "图片",
        "茅台是比较头部的。\n\n![](chat.png)\n",
        [image],
    )
    company = conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '贵州茅台'").fetchone()
    mention = conn.execute(
        "SELECT depth FROM article_mentions WHERE article_id = %s AND company_id = %s",
        (row["id"], company["id"]),
    ).fetchone()
    assert mention["depth"] == "passing"
    sources = ["fangwei-zsxq"]
    assert row["id"] not in {item["id"] for item in views_mod.pending_articles(conn, [company["id"]], sources, 20)}

    _remember(conn, row["id"], "chat.png", JUDGMENT)
    refresh_article_ocr(conn, row["id"])
    assert replace_ocr_chunks(conn, row["id"]) >= 1
    conn.commit()
    chunk = conn.execute(
        "SELECT body, anchor FROM article_chunks WHERE article_id = %s AND lang = 'ocr'",
        (row["id"],),
    ).fetchone()
    assert "短期不宜提价" in chunk["body"] and chunk["anchor"].startswith("img-")

    pending = {item["id"] for item in views_mod.pending_articles(conn, [company["id"]], sources, 20)}
    assert row["id"] in pending
    assert row["id"] in {item["id"] for item in views_mod.gap_articles(conn, [company["id"]], sources, 20)}
    assert company["id"] in {item["id"] for item in views_mod.view_companies(conn, sources)}
    companies = views_mod.pending_companies(conn, row["id"])
    assert company["id"] in {item["id"] for item in companies}

    prompts = []

    def capture_view(_settings, messages, **_kwargs):
        prompts.append(messages[1]["content"])
        return (
            '{"贵州茅台": {"about": true, "judgment": "短期不宜提价", "stance": "中性", "quote": "短期不宜提价"}}',
            "grok-test",
        )

    monkeypatch.setattr(views_mod, "chat", capture_view)
    article = conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id FROM articles WHERE id = %s",
        (row["id"],),
    ).fetchone()
    assert views_mod.extract_article(conn, settings, article, "方伟", [company]) == 1
    assert "茅台是比较头部的" in prompts[0]
    assert "【图片里的字】" in prompts[0] and "短期不宜提价" in prompts[0]
    assert "不要因为来自截图就当成没下判断" in prompts[0]
    saved_view = conn.execute(
        "SELECT judgment, ocr_hash FROM company_views WHERE article_id = %s AND company_id = %s",
        (row["id"], company["id"]),
    ).fetchone()
    ocr_hash = conn.execute("SELECT ocr_hash FROM articles WHERE id = %s", (row["id"],)).fetchone()["ocr_hash"]
    assert saved_view["judgment"] == "短期不宜提价" and saved_view["ocr_hash"] == ocr_hash

    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, prompt_version, ocr_hash)
        VALUES (%s, %s, 'test', '只写了正文那一句。', %s, '')
        """,
        (row["id"], row["content_hash"], digest_mod.PROMPT_VERSION),
    )
    conn.commit()
    assert row["id"] in {item["id"] for item in digest_mod.pending_articles(conn, sources, 20)}
    digest_row = conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id, ocr_hash FROM articles WHERE id = %s",
        (row["id"],),
    ).fetchone()
    stored = {"prompt_version": digest_mod.PROMPT_VERSION, "head_only": False, "ocr_hash": ""}
    assert digest_mod.digest_due(digest_row, stored)

    seen = []

    def capture_digest(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary": "导读", "points": ["短期不宜提价"], "tags": ["贵州茅台"]}', "grok-test"

    monkeypatch.setattr(digest_mod, "chat", capture_digest)
    digest_mod.digest_article(conn, settings, digest_row, "方伟")
    assert "茅台是比较头部的" in seen[0]
    assert "【图片里的字】" in seen[0] and "短期不宜提价" in seen[0]
    assert "不要只概括正文里那一句" in seen[0]


def test_long_article_passing_mention_is_not_queued_for_a_screenshot(conn, settings, tmp_path):
    image = tmp_path / "chart.png"
    _png(image, b"\x03" * 24)
    filler = "\n\n".join(["今天只是在记盘面，没有要展开的公司。" * 4 for _ in range(8)])
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-long",
        "长文顺带一提",
        filler + "\n\n顺带提一句茅台。\n\n![](chart.png)\n",
        [image],
    )
    _remember(conn, row["id"], "chart.png", JUDGMENT)
    refresh_article_ocr(conn, row["id"])
    conn.commit()
    body_len = conn.execute("SELECT length(body_text) AS n FROM articles WHERE id = %s", (row["id"],)).fetchone()["n"]
    assert body_len >= 400
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    depth = conn.execute(
        "SELECT depth FROM article_mentions WHERE article_id = %s AND company_id = %s",
        (row["id"], company),
    ).fetchone()["depth"]
    assert depth == "passing"
    assert row["id"] not in {item["id"] for item in views_mod.pending_articles(conn, [company], ["fangwei-zsxq"], 20)}
    assert row["id"] not in {item["id"] for item in views_mod.gap_articles(conn, [company], ["fangwei-zsxq"], 20)}

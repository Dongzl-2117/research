"""公司页的观点和接口 /views 列出的标题，和卡片、文章页同一套：只有截图的星球帖按图片里的字补标题。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib import views as views_mod
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.mentions import rebuild_article
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

OCR = "张三：美团外卖守住了\n方伟：拼多多今年的利润会比市场想的高"
SAID = "拼多多今年的利润会比市场想的高"


def test_view_titles_match_the_card_for_a_screenshot_post(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    # 标题只有表情（占位），正文只有「图片」，话都在截图里。
    doc = ParsedDoc("92001", "😃", datetime(2026, 10, 30, 9, 0, tzinfo=SHANGHAI), "「图片」",
                    {"topic_id": "92001", "poster_name": "方伟Rey"}, [])
    store_documents(conn, source=sources["fangwei-zsxq"], docs=[doc], data_dir=settings.data_dir,
                    archive_files=1, images_in_archive=0, errors=[])
    row = conn.execute("SELECT id, content_hash FROM articles WHERE external_id = '92001'").fetchone()
    conn.execute("UPDATE articles SET ocr_text = %s WHERE id = %s", (OCR, row["id"]))
    rebuild_article(conn, row["id"])
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '拼多多'").fetchone()["id"]
    conn.execute(
        "INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance, about)"
        " VALUES (%s, %s, %s, %s, %s, 'positive', true)",
        (row["id"], company, row["content_hash"], views_mod.PROMPT_VERSION, "利润会比市场想的高"),
    )
    conn.commit()
    # 时间线自己只看正文，补出来的是日期占位。
    assert [item["title"] for item in views_mod.timeline(conn, company, sources, settings)] == ["2026-10-30 短帖"]

    with TestClient(create_app(settings)) as client:
        card = client.get(f"/api/v1/articles/{row['id']}").json()["title"]
        views = client.get(f"/api/v1/companies/{company}/views").json()["views"]
        page = client.get(f"/companies/{company}", params={"tab": "views"}).text
    assert card == SAID
    assert [(item["article_id"], item["title"]) for item in views] == [(row["id"], SAID)]
    section = page.split('id="views"', 1)[1]
    assert f'<span class="jrow-src">{SAID}</span>' in section
    assert "2026-10-30 短帖" not in section

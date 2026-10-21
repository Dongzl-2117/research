"""方伟星球卡片和文章页不显示被省略号截断的标题。库里的原标题先不动。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

FULL = "大模型生意的护城河在哪里，明确还不是一个能够回答的话题"
TRUNCATED = "大模型生意的护城河在哪里，明确…"
# 去掉省略号并剪掉没说完的「，明确」之后，原标题还是一句可用的话，不再用摘要补全。
GUEST_TITLE = "大模型生意的护城河在哪里"
BODY = FULL + "。\n\n作者没有写出为什么现在答不了。\n"
DIGEST = "方伟认为，" + FULL + "。正文只给出这一判断，没有说明依据。"


def _source(settings):
    return next(source for source in load_sources(settings.sources_path) if source.id == "fangwei-zsxq")


def test_fangwei_cards_and_article_replace_cut_off_titles(conn, settings):
    when = datetime(2024, 10, 19, 10, 58, tzinfo=SHANGHAI)
    guest = ParsedDoc(
        "reader-cut",
        TRUNCATED,
        when,
        TRUNCATED + "\n",
        {"format": "markdown", "filename": "reader.md", "poster_name": "方伟Rey"},
        [],
    )
    owner = ParsedDoc(
        "owner-cut",
        TRUNCATED,
        datetime(2024, 10, 19, 9, 0, tzinfo=SHANGHAI),
        BODY,
        {"format": "markdown", "filename": "owner.md"},
        [],
    )
    store_documents(
        conn,
        source=_source(settings),
        docs=[guest, owner],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    guest_row = conn.execute(
        "SELECT id, title, content_hash FROM articles WHERE external_id = 'reader-cut'"
    ).fetchone()
    assert guest_row["title"] == TRUNCATED
    conn.execute(
        """
        INSERT INTO article_digests (
          article_id, content_hash, model, summary, points, tags, prompt_version, head_only, ocr_hash
        )
        VALUES (%s, %s, 'test', %s, '[]'::jsonb, '[]'::jsonb, 2, false, '')
        """,
        (guest_row["id"], guest_row["content_hash"], DIGEST),
    )
    conn.commit()
    owner_row = conn.execute("SELECT id, title FROM articles WHERE external_id = 'owner-cut'").fetchone()
    assert owner_row["title"] == TRUNCATED
    with TestClient(create_app(settings)) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert home.text.count(FULL) >= 2
        assert "明确…" not in home.text
        assert "读者帖" in home.text
        assert "方伟Rey" in home.text
        assert "research-recent-views" in home.text
        guest_page = client.get(f"/articles/{guest_row['id']}")
        assert guest_page.status_code == 200
        assert f"<h1>{GUEST_TITLE}</h1>" in guest_page.text
        assert "<title>" + GUEST_TITLE in guest_page.text
        assert f"<h1>{FULL}</h1>" not in guest_page.text
        owner_page = client.get(f"/articles/{owner_row['id']}")
        assert owner_page.status_code == 200
        assert f"<h1>{FULL}</h1>" in owner_page.text
        assert "明确…" not in owner_page.text
    stored = conn.execute("SELECT title FROM articles WHERE external_id = 'reader-cut'").fetchone()
    assert stored["title"] == TRUNCATED

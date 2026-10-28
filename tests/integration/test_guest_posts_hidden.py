"""读者帖不进网站展示：首页、按天、检索、正文都 404/看不到。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app


def _source(settings, source_id):
    return next(source for source in load_sources(settings.sources_path) if source.id == source_id)


def test_guest_posts_hidden_from_site(conn, settings):
    zsxq = _source(settings, "fangwei-zsxq")
    when = datetime(2026, 10, 27, 16, 52, tzinfo=SHANGHAI)
    store_documents(
        conn,
        source=zsxq,
        docs=[
            ParsedDoc(
                external_id="guest-bubble",
                title="泡泡玛特三季度腰斩。",
                published_at=when,
                body_md="泡泡玛特三季度腰斩。\n",
                raw_meta={
                    "format": "markdown",
                    "filename": "guest-bubble.md",
                    "poster_name": "Simon",
                    "poster_role": "guest",
                    "topic_id": "guest-bubble",
                },
                image_refs=[],
            ),
            ParsedDoc(
                external_id="owner-bubble",
                title="泡泡玛特：",
                published_at=when.replace(hour=18, minute=55),
                body_md="星主自己写的一句。\n",
                raw_meta={
                    "format": "markdown",
                    "filename": "owner-bubble.md",
                    "poster_name": "方伟Rey",
                    "poster_role": "owner",
                    "topic_id": "owner-bubble",
                },
                image_refs=[],
            ),
        ],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    guest_id = conn.execute(
        "SELECT id FROM articles WHERE external_id = %s", ("guest-bubble",)
    ).fetchone()["id"]
    owner_id = conn.execute(
        "SELECT id FROM articles WHERE external_id = %s", ("owner-bubble",)
    ).fetchone()["id"]

    app = create_app(settings)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "泡泡玛特三季度腰斩" not in home.text
        assert "Simon" not in home.text
        assert "读者帖" not in home.text
        assert "泡泡玛特：" in home.text

        day = client.get("/days/2026-10-27")
        assert day.status_code == 200
        assert "泡泡玛特三季度腰斩" not in day.text
        assert "读者帖" not in day.text

        found = client.get("/search", params={"q": "腰斩"})
        assert found.status_code == 200
        assert "泡泡玛特三季度腰斩" not in found.text

        assert client.get(f"/articles/{guest_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{guest_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{guest_id}.md").status_code == 404
        assert client.get(f"/articles/{owner_id}").status_code == 200
        assert client.get(f"/api/v1/articles/{owner_id}").status_code == 200


def test_guest_and_comment_shell_api_404(conn, settings):
    """API / .md 与 HTML 对齐：guest 与「图片」评论壳都 404。"""
    zsxq = _source(settings, "fangwei-zsxq")
    when = datetime(2026, 10, 27, 12, 0, tzinfo=SHANGHAI)
    store_documents(
        conn,
        source=zsxq,
        docs=[
            ParsedDoc(
                external_id="guest-api-hide",
                title="读者随口一问",
                published_at=when,
                body_md="读者写的一句。\n",
                raw_meta={
                    "format": "markdown",
                    "filename": "guest-api.md",
                    "poster_name": "某读者",
                    "poster_role": "guest",
                    "topic_id": "guest-api-hide",
                },
                image_refs=[],
            ),
            ParsedDoc(
                external_id="shell-9182-like",
                title="「图片」 评论（5） - 某读者",
                published_at=when.replace(hour=13),
                body_md="「图片」\n\n## 评论里的回复\n\n作者答了一句。\n",
                raw_meta={
                    "format": "markdown",
                    "filename": "shell.md",
                    "poster_name": "方伟Rey",
                    "poster_role": "owner",
                    "topic_id": "shell-9182-like",
                },
                image_refs=[],
            ),
            ParsedDoc(
                external_id="owner-api-keep",
                title="星主正经帖",
                published_at=when.replace(hour=14),
                body_md="这是星主自己的长文判断，多写一点免得被当成空壳。\n",
                raw_meta={
                    "format": "markdown",
                    "filename": "owner.md",
                    "poster_name": "方伟Rey",
                    "poster_role": "owner",
                    "topic_id": "owner-api-keep",
                },
                image_refs=[],
            ),
        ],
        data_dir=settings.data_dir,
        archive_files=3,
        images_in_archive=0,
        errors=[],
    )
    guest_id = conn.execute(
        "SELECT id FROM articles WHERE external_id = %s", ("guest-api-hide",)
    ).fetchone()["id"]
    shell_id = conn.execute(
        "SELECT id FROM articles WHERE external_id = %s", ("shell-9182-like",)
    ).fetchone()["id"]
    owner_id = conn.execute(
        "SELECT id FROM articles WHERE external_id = %s", ("owner-api-keep",)
    ).fetchone()["id"]

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get(f"/articles/{guest_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{guest_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{guest_id}.md").status_code == 404

        assert client.get(f"/articles/{shell_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{shell_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{shell_id}.md").status_code == 404

        home = client.get("/")
        assert home.status_code == 200
        assert "「图片」 评论（5）" not in home.text
        assert "某读者" not in home.text
        assert "星主正经帖" in home.text

        assert client.get(f"/articles/{owner_id}").status_code == 200
        assert client.get(f"/api/v1/articles/{owner_id}").status_code == 200
        assert client.get(f"/api/v1/articles/{owner_id}.md").status_code == 200

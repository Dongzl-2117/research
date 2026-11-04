"""星球帖开头点名回复的网友（「@王小明 我后面会写……」）：卡片、文章页和接口的标题都不带昵称，
接口原文开头也不再顶着导出的旧标题行。网友名字用虚构的。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

TITLE = "@王小明 我后面会写一篇更细的来分析一下这个财报"
BODY = (
    f"# {TITLE}\n\n- 作者：方伟Rey\n- 时间：2026-08-21 11:24 (UTC+8)\n\n{TITLE}\n\n"
    "泡泡玛特最值钱的是无形的IP资产，资产轻，所以ROE高。\n"
)
CLEAN = "我后面会写一篇更细的来分析一下这个财报"


def test_reader_mention_stays_out_of_titles(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-zsxq")
    when = datetime(2026, 8, 21, 11, 24, tzinfo=SHANGHAI)
    doc = ParsedDoc("80001", TITLE, when, BODY, {"topic_id": "80001", "poster_name": "方伟Rey"}, [])
    store_documents(
        conn, source=source, docs=[doc], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[]
    )
    article_id = conn.execute("SELECT id FROM articles WHERE external_id = '80001'").fetchone()["id"]

    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        page = client.get(f"/articles/{article_id}").text
        data = client.get(f"/api/v1/articles/{article_id}").json()
        markdown = client.get(f"/api/v1/articles/{article_id}.md").text
    assert CLEAN in home and TITLE not in home
    assert f"<h1>{CLEAN}</h1>" in page
    # 正文里星主那句点名留着，只是不再在正文顶上变成第二个大标题。
    assert "<h2>@王小明" not in page and "<h1>@王小明" not in page
    assert data["title"] == CLEAN
    assert not data["body_markdown"].lstrip().startswith("#")
    assert f'title: "{CLEAN}"' in markdown
    assert [line for line in markdown.splitlines() if line.startswith("# ")] == [f"# {CLEAN}"]

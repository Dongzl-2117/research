"""只有图的星球帖，库里的标题是读者的一条评论（网名、评论时间加那句话，线上 23834）：卡片、文章页和接口
都不显示这个标题，也不从正文里读者的评论另挑一句。网名用虚构的。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

TITLE = "王小明（2026-02-02 21:57） 虽然不体现在资产负债表"
# 早先入库的帖子，正文里还留着读者的评论；文章页的评论区显示时才滤掉。
BODY = (
    "「图片」\n\n## 评论（2）\n\n"
    "- **王小明**（2026-02-02 21:57）\n  虽然不体现在资产负债表，但是这块业务的现金流很好。\n"
    "- **李四**（2026-02-02 22:10）\n  同意。\n"
)


def test_reader_comment_title_is_not_shown(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-zsxq")
    when = datetime(2026, 2, 2, 21, 50, tzinfo=SHANGHAI)
    doc = ParsedDoc("80002", TITLE, when, BODY, {"topic_id": "80002", "poster_name": "方伟Rey"}, [])
    store_documents(
        conn, source=source, docs=[doc], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[]
    )
    row = conn.execute("SELECT id, title, body_text FROM articles WHERE external_id = '80002'").fetchone()
    assert row["title"] == TITLE and "资产负债表" in row["body_text"]

    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        page = client.get(f"/articles/{row['id']}").text
        data = client.get(f"/api/v1/articles/{row['id']}").json()
    assert "2026-02-02 短帖" in home
    assert "<h1>2026-02-02 短帖</h1>" in page
    assert data["title"] == "2026-02-02 短帖"
    for text in (home, page, data["title"]):
        assert "王小明" not in text and "资产负债表" not in text and "同意" not in text

"""星球问答帖的「提问（网友 → 方伟Rey）」不进检索的章节名和标题：新切的块、迁移 026 改掉的旧块、检索页。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.migrate import MIGRATIONS, split_sql
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

# 形状对齐 10315：「# 问：」一行、归档头、「## 提问（网友 → 方伟Rey）」「## 回答（方伟Rey）」。网友名字用虚构的。
QA = """# 问：请教一下击球区怎么判断

- 作者：王小明
- 时间：2025-04-29 16:23 (UTC+8)
- 类型：q&a
- topic_id：`70001` · 原链接：https://wx.zsxq.com/topic/70001

## 提问（王小明 → 方伟Rey）

请教一下击球区怎么判断

## 回答（方伟Rey）

击球区先看价格和十年后的现金流，蜜雪冰城还要再等一年的同店数据。
"""
NOTES = "# 读书笔记\n\n## 回答（续）\n\n上回没说完的击球区，再补两句。\n"


def _headings(conn, external_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT c.heading FROM article_chunks c JOIN articles a ON a.id = c.article_id"
        " WHERE a.external_id = %s AND c.lang = '' ORDER BY c.ord",
        (external_id,),
    ).fetchall()
    return [row["heading"] for row in rows]


def test_qa_asker_name_stays_out_of_chunk_headings_and_search(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-zsxq")
    meta = {"topic_id": "70001", "poster_name": "方伟Rey"}
    docs = [
        ParsedDoc("70001", "", datetime(2025, 4, 29, 16, 23, tzinfo=SHANGHAI), QA, meta, []),
        ParsedDoc("70002", "读书笔记", datetime(2025, 4, 30, 9, 0, tzinfo=SHANGHAI), NOTES, {}, []),
    ]
    store_documents(
        conn, source=source, docs=docs, data_dir=settings.data_dir, archive_files=2, images_in_archive=0, errors=[]
    )
    assert _headings(conn, "70001")[-2:] == ["提问", "回答"]
    assert _headings(conn, "70002") == ["回答（续）"]

    # 迁移 026 以前切的块：章节名还带着网友的昵称。
    conn.execute(
        """
        UPDATE article_chunks c
        SET heading = CASE c.heading WHEN '提问' THEN '提问（王小明 → 方伟Rey）' ELSE '回答（方伟Rey）' END
        FROM articles a
        WHERE a.id = c.article_id AND a.external_id = '70001' AND c.heading IN ('提问', '回答')
        """
    )
    for _ in range(2):
        for statement in split_sql((MIGRATIONS / "026_qa_chunk_headings.sql").read_text(encoding="utf-8")):
            conn.execute(statement)
    assert _headings(conn, "70001")[-2:] == ["提问", "回答"]
    # 没有「提问（…）」配对的小标题不动。
    assert _headings(conn, "70002") == ["回答（续）"]
    conn.commit()

    with TestClient(create_app(settings)) as client:
        page = client.get("/search", params={"q": "击球区"}).text
    assert "问：请教一下击球区怎么判断" in page
    assert "王小明" not in page

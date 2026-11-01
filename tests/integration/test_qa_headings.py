"""星球问答帖提问网友的昵称不露在网站上：「提问（网友 → 方伟Rey）」不进检索的章节名和标题（新切的块、迁移 026
改掉的旧块、检索页），也不进检索文字、卡片和接口的摘录、接口原文和改动页（迁移 027 改掉以前入库的）。"""

import re
from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.migrate import MIGRATIONS, split_sql
from researchlib.revisions import save_revision
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


def _store(conn, settings) -> None:
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-zsxq")
    meta = {"topic_id": "70001", "poster_name": "方伟Rey"}
    docs = [
        ParsedDoc("70001", "", datetime(2025, 4, 29, 16, 23, tzinfo=SHANGHAI), QA, meta, []),
        ParsedDoc("70002", "读书笔记", datetime(2025, 4, 30, 9, 0, tzinfo=SHANGHAI), NOTES, {}, []),
    ]
    store_documents(
        conn, source=source, docs=docs, data_dir=settings.data_dir, archive_files=2, images_in_archive=0, errors=[]
    )


def _migrate(conn, name: str) -> None:
    for _ in range(2):
        for statement in split_sql((MIGRATIONS / name).read_text(encoding="utf-8")):
            conn.execute(statement)


def _article(conn, external_id: str):
    return conn.execute("SELECT id, body_text FROM articles WHERE external_id = %s", (external_id,)).fetchone()


def _headings(conn, external_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT c.heading FROM article_chunks c JOIN articles a ON a.id = c.article_id"
        " WHERE a.external_id = %s AND c.lang = '' ORDER BY c.ord",
        (external_id,),
    ).fetchall()
    return [row["heading"] for row in rows]


def test_qa_asker_name_stays_out_of_chunk_headings_and_search(conn, settings):
    _store(conn, settings)
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
    _migrate(conn, "026_qa_chunk_headings.sql")
    assert _headings(conn, "70001")[-2:] == ["提问", "回答"]
    # 没有「提问（…）」配对的小标题不动。
    assert _headings(conn, "70002") == ["回答（续）"]
    conn.commit()

    with TestClient(create_app(settings)) as client:
        page = client.get("/search", params={"q": "击球区"}).text
    # 检索页的标题标出了命中的词，去掉标记再比。
    assert "问：请教一下击球区怎么判断" in re.sub(r"</?mark>", "", page)
    assert "王小明" not in page


def test_qa_asker_name_stays_out_of_body_text_excerpts_api_and_revisions(conn, settings):
    _store(conn, settings)
    qa, notes = _article(conn, "70001"), _article(conn, "70002")
    assert "王小明" not in qa["body_text"] and "\n提问\n" in qa["body_text"] and "\n回答\n" in qa["body_text"]
    assert "回答（续）" in notes["body_text"]

    # 迁移 027 以前入库的：检索文字和改动记录里存的旧版都还带着昵称。
    old = qa["body_text"].replace("\n提问\n", "\n提问（王小明 → 方伟Rey）\n").replace("\n回答\n", "\n回答（方伟Rey）\n")
    conn.execute("UPDATE articles SET body_text = %s WHERE id = %s", (old, qa["id"]))
    save_revision(
        conn,
        qa["id"],
        kind="held",
        reason="shrunk",
        content_hash="older",
        title="",
        published_at=None,
        body_md=QA.replace("十年后", "五年后"),
        body_text=old.replace("十年后", "五年后"),
        raw_meta={},
    )
    _migrate(conn, "027_qa_body_text.sql")
    assert _article(conn, "70001")["body_text"] == qa["body_text"]
    held = conn.execute("SELECT body_text FROM article_revisions WHERE article_id = %s", (qa["id"],)).fetchone()
    assert held["body_text"] == qa["body_text"].replace("十年后", "五年后")
    # 没有「提问（…）」配对的不动。
    assert _article(conn, "70002")["body_text"] == notes["body_text"]
    conn.commit()

    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        listing = client.get("/api/v1/articles", params={"author": "方伟"}).text
        single = client.get(f"/api/v1/articles/{qa['id']}").json()
        markdown = client.get(f"/api/v1/articles/{qa['id']}.md").text
        revisions = client.get(f"/articles/{qa['id']}/revisions").text
    assert "击球区先看价格" in home and "王小明" not in home
    assert "击球区先看价格" in listing and "王小明" not in listing
    assert "王小明" not in single["body_markdown"] and "## 提问\n" in single["body_markdown"]
    assert "- 时间：2025-04-29 16:23" in single["body_markdown"]
    assert "王小明" not in markdown and "## 回答\n" in markdown
    assert "五年后" in revisions and "王小明" not in revisions

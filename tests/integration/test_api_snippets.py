"""接口列表的摘录和公司提及原句：不重复标题、不带归档头，按整句取，截图帖从图片里的字找提及。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.mentions import rebuild_article
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

HEADLINE = "贵州茅台：第三季度净利润199.51亿元，同比增长3.78%"
NEWS = (
    f"{HEADLINE}\n\n在这么差的Q3大环境还能增长就厉害了。"
    + "前面铺垫的话很多。" * 12
    + "拼多多的百亿补贴还在继续，对白酒价格的压力不小。后面再说别的。\n"
)
SHOT = "美团"
OCR = "张三：美团外卖守住了\n方伟：拼多多今年的利润会比市场想的高"


def _store(conn, settings, external_id, title, body, hour):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-zsxq")
    when = datetime(2026, 10, 30, hour, 0, tzinfo=SHANGHAI)
    doc = ParsedDoc(external_id, title, when, body, {"topic_id": external_id, "poster_name": "方伟Rey"}, [])
    store_documents(
        conn, source=source, docs=[doc], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[]
    )
    return conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()["id"]


def test_list_opening_and_company_mention_sentence(conn, settings):
    news = _store(conn, settings, "90001", HEADLINE, NEWS, 9)
    shot = _store(conn, settings, "90002", SHOT, "「图片」", 10)
    # 只有图的帖：提及只在图片里的字里（短正文的提及连图片里的字一起算）。
    conn.execute("UPDATE articles SET ocr_text = %s WHERE id = %s", (OCR, shot))
    rebuild_article(conn, shot)
    conn.commit()
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '拼多多'").fetchone()["id"]
    assert conn.execute(
        "SELECT count(*) AS n FROM article_mentions WHERE company_id = %s AND article_id = ANY(%s)",
        (company, [news, shot]),
    ).fetchone()["n"] == 2

    with TestClient(create_app(settings)) as client:
        day = client.get("/api/v1/days/2026-10-30").json()
        data = client.get(f"/api/v1/companies/{company}").json()
        page = client.get(f"/companies/{company}", params={"depth": "all"}).text
    snippets = {item["id"]: item["snippet"] for item in day["items"]}
    # 标题就是正文第一行：摘录从下一句开始，不再把标题重复一遍。
    assert snippets[news].startswith("在这么差的Q3大环境还能增长就厉害了。")
    assert HEADLINE not in snippets[news]

    contexts = {item["id"]: item["snippet"] for item in data["items"]}
    # 提及原句从句首取，不从半句开始。
    assert contexts[news] == "拼多多的百亿补贴还在继续，对白酒价格的压力不小。"
    assert contexts[shot] == "方伟：拼多多今年的利润会比市场想的高。"
    assert "<mark>拼多多</mark>的百亿补贴还在继续，对白酒价格的压力不小。" in page
    assert "方伟：<mark>拼多多</mark>今年的利润会比市场想的高。" in page

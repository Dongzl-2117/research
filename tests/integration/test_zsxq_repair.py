"""星球存量：截断标题回写、别人的评论清掉、方伟星球的留言区 OCR 重洗。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI, content_hash
from researchlib.web.app import create_app
from researchlib.zsxq_comments import source_owners
from researchlib.zsxq_repair import repair_zsxq
from researchlib.zsxq_title import title_needs_repair

FULL = "大模型生意的护城河在哪里，明确还不是一个能够回答的话题"
TRUNCATED = "大模型生意的护城河在哪里，明确…"
BODY = FULL + "。\n\n作者没有写出为什么现在答不了。\n"
COMMENTS = """正文一句，讲的是渠道库存。

## 评论（2）

- **陈小东**（2026-10-21 11:42）
  个人觉得不该现在看多。
- **方伟Rey**（2026-10-21 11:50）
  切换成本还在。
"""
SHOT = """方伟
茅台批价这一周没动，渠道仍在观望。
李四留言
渠道库存还是偏高。
3条留言
划线人数 18
赞赏
"""


def _source(settings):
    return next(source for source in load_sources(settings.sources_path) if source.id == "fangwei-zsxq")


def test_repair_rewrites_title_comments_and_ocr_without_restamping_views(conn, settings):
    when = datetime(2024, 10, 19, 10, 0, tzinfo=SHANGHAI)
    store_documents(
        conn,
        source=_source(settings),
        docs=[
            ParsedDoc("cut-title", TRUNCATED, when, BODY, {"format": "markdown"}, []),
            ParsedDoc("guest-comments", "渠道库存还在", when, COMMENTS, {"format": "markdown"}, []),
            ParsedDoc("ocr-footer", "图片", when, "（见图）\n", {"format": "markdown"}, []),
        ],
        data_dir=settings.data_dir,
        archive_files=3,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    cut = conn.execute("SELECT id, title, content_hash FROM articles WHERE external_id = 'cut-title'").fetchone()
    comments = conn.execute(
        "SELECT id, body_md, content_hash FROM articles WHERE external_id = 'guest-comments'"
    ).fetchone()
    ocr_row = conn.execute("SELECT id, content_hash FROM articles WHERE external_id = 'ocr-footer'").fetchone()
    assert cut["title"] == TRUNCATED
    assert "陈小东" in comments["body_md"]
    image_hash = "ab" * 32
    conn.execute(
        "INSERT INTO image_texts (content_hash, engine, text) VALUES (%s, 'test', %s)",
        (image_hash, SHOT),
    )
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        VALUES (%s, 'fangwei-zsxq', 'shot.png', 'fangwei-zsxq/ab/shot.png', %s)
        """,
        (ocr_row["id"], image_hash),
    )
    conn.execute(
        "UPDATE articles SET ocr_text = %s, ocr_hash = 'old' WHERE id = %s",
        (SHOT, ocr_row["id"]),
    )
    company = conn.execute("SELECT id FROM companies ORDER BY id LIMIT 1").fetchone()
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
        VALUES (%s, %s, %s, 1, '护城河还说不清', 'neutral')
        """,
        (cut["id"], company["id"], cut["content_hash"]),
    )
    conn.commit()

    stats = repair_zsxq(conn, spot_ids=[cut["id"], comments["id"], ocr_row["id"]])
    conn.commit()

    assert stats["titles"] >= 1
    assert stats["comments_dirty"] == 1
    assert stats["comments_cleaned"] == 1
    assert stats["ocr_rewash"] == 1
    retitled = conn.execute(
        "SELECT title, content_hash, body_md, published_at FROM articles WHERE id = %s",
        (cut["id"],),
    ).fetchone()
    assert retitled["title"] == FULL
    assert not title_needs_repair(retitled["title"])
    assert not retitled["title"].endswith("…")
    view = conn.execute(
        "SELECT content_hash, judgment FROM company_views WHERE article_id = %s",
        (cut["id"],),
    ).fetchone()
    assert view["content_hash"] == retitled["content_hash"]
    assert view["judgment"] == "护城河还说不清"
    assert view["content_hash"] == content_hash(FULL, retitled["body_md"], retitled["published_at"])
    cleaned = conn.execute("SELECT body_md FROM articles WHERE id = %s", (comments["id"],)).fetchone()
    assert "陈小东" not in cleaned["body_md"]
    assert "方伟Rey" in cleaned["body_md"] and "切换成本还在" in cleaned["body_md"]
    ocr = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (ocr_row["id"],)).fetchone()
    assert ocr["ocr_text"].startswith("方伟：")
    assert "李四" not in ocr["ocr_text"] and "条留言" not in ocr["ocr_text"] and "赞赏" not in ocr["ocr_text"]
    spots = {item["id"]: item for item in stats["spots"]}
    assert spots[cut["id"]]["title_truncated"] is False
    assert spots[comments["id"]]["comment"] == "评论只剩作者"
    assert spots[ocr_row["id"]]["chrome_under_host"] is False

    again = repair_zsxq(conn, spot_ids=[])
    assert again["titles"] == 0
    assert again["comments_cleaned"] == 0
    assert again["ocr_rewash"] == 0


def test_other_zsxq_sources_keep_their_own_host_comments(conn, settings):
    """乘风破浪：评论区留它自己的星主，读者的评论在阅读页、接口里不露，回写后检索也搜不到。图片文字不重洗。"""
    source = next(item for item in load_sources(settings.sources_path) if item.id == "chengfengpolang")
    when = datetime(2026, 9, 27, 14, 41, tzinfo=SHANGHAI)
    body = (
        "Temu：值得重视的关税利好\n\n"
        "中美这次国事访问后达成的对等降税安排，主要是针对几类商品：小家电、玩具、节庆装饰品等。\n\n"
        "## 评论（3）\n\n"
        "- **王小明**（2026-09-27 15:00）\n  同意，半托管的灯笼订单早就铺开了。\n"
        # 星主在楼中楼里回答读者：导出写成「乘风破浪 回复 王小明」，要留，昵称不留。
        "  - **乘风破浪 回复 王小明**（2026-09-27 15:05）\n    节庆装饰的单子三季度才放量。\n"
        "- **乘风破浪（作者）**（2026-09-27 15:10）\n  铺开了，这次降税对小家电最直接。\n"
    )
    store_documents(
        conn,
        source=source,
        docs=[ParsedDoc("cf-1250", "Temu：值得重视的关税利好 ...", when, body, {"format": "markdown"}, [])],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    article_id = conn.execute("SELECT id FROM articles WHERE external_id = 'cf-1250'").fetchone()["id"]

    # 库里还没清：阅读页、接口按来源的星主现滤。检索块还是旧的，读者的话搜得到。
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}").text
        api_md = client.get(f"/api/v1/articles/{article_id}.md").text
        assert f"/articles/{article_id}" in client.get("/search", params={"q": "灯笼订单"}).text
    for shown in (page, api_md):
        assert "这次降税对小家电最直接" in shown
        assert "节庆装饰的单子三季度才放量" in shown and "乘风破浪 回复网友" in shown
        assert "王小明" not in shown and "灯笼订单" not in shown

    stats = repair_zsxq(conn, "chengfengpolang", titles_only=True)
    conn.commit()
    assert stats["titles"] == 1 and stats["spots"] == []
    row = conn.execute("SELECT title, body_md FROM articles WHERE id = %s", (article_id,)).fetchone()
    assert row["title"] == "Temu：值得重视的关税利好"
    assert not title_needs_repair(row["title"])
    assert "王小明" in row["body_md"]

    owners = {item.id: source_owners(item) for item in load_sources(settings.sources_path)}
    stats = repair_zsxq(
        conn, "chengfengpolang", owners=owners["chengfengpolang"], spot_owners=owners, spot_ids=[article_id]
    )
    conn.commit()
    assert stats["comments_dirty"] == 1 and stats["comments_cleaned"] == 1
    assert stats["ocr_rewash"] == 0 and stats["ocr_rewash_mp"] == 0
    assert stats["spots"][0]["comment"] == "评论只剩作者"
    row = conn.execute("SELECT body_md, body_text FROM articles WHERE id = %s", (article_id,)).fetchone()
    assert "王小明" not in row["body_md"] and "灯笼订单" not in row["body_text"]
    assert "这次降税对小家电最直接" in row["body_md"]
    assert "- **乘风破浪 回复网友**（2026-09-27 15:05）" in row["body_md"]
    assert "节庆装饰的单子三季度才放量" in row["body_text"]
    chunks = " ".join(
        item["body"]
        for item in conn.execute("SELECT body FROM article_chunks WHERE article_id = %s", (article_id,)).fetchall()
    )
    assert "灯笼订单" not in chunks and "这次降税对小家电最直接" in chunks
    with TestClient(create_app(settings)) as client:
        assert f"/articles/{article_id}" not in client.get("/search", params={"q": "灯笼订单"}).text

    again = repair_zsxq(conn, "chengfengpolang", owners=owners["chengfengpolang"])
    assert again["comments_cleaned"] == 0

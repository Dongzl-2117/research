"""方伟星球存量：截断标题回写、非方伟评论清掉、留言区 OCR 重洗。"""

from datetime import datetime

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI, content_hash
from researchlib.zsxq_repair import repair_zsxq
from researchlib.zsxq_title import title_needs_repair

FULL = "大模型生意的护城河在哪里，明确还不是一个能够回答的话题"
TRUNCATED = "大模型生意的护城河在哪里，明确…"
BODY = FULL + "。\n\n作者没有写出为什么现在答不了。\n"
COMMENTS = """正文一句，讲的是渠道库存。

## 评论（2）

- **曾广文**（2026-10-21 11:42）
  个人觉得不该现在看多。
- **方伟Rey**（2026-10-21 11:50）
  切换成本还在。
"""
SHOT = """方伟
茅台批价这一周没动，渠道仍在观望。
张冠留言
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
    assert "曾广文" in comments["body_md"]
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
    assert "曾广文" not in cleaned["body_md"]
    assert "方伟Rey" in cleaned["body_md"] and "切换成本还在" in cleaned["body_md"]
    ocr = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (ocr_row["id"],)).fetchone()
    assert ocr["ocr_text"].startswith("方伟：")
    assert "张冠" not in ocr["ocr_text"] and "条留言" not in ocr["ocr_text"] and "赞赏" not in ocr["ocr_text"]
    spots = {item["id"]: item for item in stats["spots"]}
    assert spots[cut["id"]]["title_truncated"] is False
    assert spots[comments["id"]]["comment"] == "评论只剩方伟"
    assert spots[ocr_row["id"]]["chrome_under_host"] is False

    again = repair_zsxq(conn, spot_ids=[])
    assert again["titles"] == 0
    assert again["comments_cleaned"] == 0
    assert again["ocr_rewash"] == 0

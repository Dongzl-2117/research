"""短正文的图片文字和摘要对不上时，不能排在「还没有摘要」的积压后面。"""

from researchlib import digest as digest_mod
from researchlib.followups import note_ocr_changes
from researchlib.zsxq_title import title_needs_ai

SHELL = "正文除标题「图片」外没有可读文字，无法提炼作者的核心判断及其依据"


def test_ocr_stale_short_digest_comes_before_a_newer_missing_one(conn):
    conn.execute(
        """
        INSERT INTO articles (
          source_id, external_id, content_hash, title, published_at, body_md, body_text, ocr_hash, ocr_text
        ) VALUES
          ('fangwei-zsxq', 'missing-digest', 'm', '还没有摘要', '2026-10-29 12:00+08', '正文', '正文', '', ''),
          ('fangwei-zsxq', 'ocr-stale', 's', %s, '2026-10-01 12:00+08', '图片', '图片', 'newhash', '方伟：茅台')
        """,
        (SHELL,),
    )
    stale = conn.execute(
        "SELECT id, content_hash FROM articles WHERE external_id = 'ocr-stale'"
    ).fetchone()
    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, prompt_version, head_only, ocr_hash)
        VALUES (%s, %s, 'test', %s, %s, false, '')
        """,
        (stale["id"], stale["content_hash"], SHELL, digest_mod.PROMPT_VERSION),
    )
    conn.commit()
    pending = [row["id"] for row in digest_mod.pending_articles(conn, ["fangwei-zsxq"], 10)]
    assert pending[0] == stale["id"]
    assert title_needs_ai(SHELL)


def test_ocr_hash_change_requeues_title_even_when_the_title_looks_done(conn):
    conn.execute(
        """
        INSERT INTO articles (
          source_id, external_id, content_hash, title, published_at, body_md, body_text
        ) VALUES (
          'fangwei-zsxq', 'good-title', 'g', '关于携程与Booking', '2026-10-29 14:00+08', '图片', '图片'
        )
        """
    )
    row = conn.execute("SELECT id FROM articles WHERE external_id = 'good-title'").fetchone()
    found = note_ocr_changes(
        conn,
        [
            {
                "id": row["id"],
                "source_id": "fangwei-zsxq",
                "body_len": 2,
                "old_hash": "",
                "new_hash": "abc",
            }
        ],
    )
    conn.commit()
    kinds = {
        item["kind"]
        for item in conn.execute(
            "SELECT kind FROM article_followups WHERE article_id = %s",
            (row["id"],),
        ).fetchall()
    }
    assert kinds == {"digest", "zsxq_title"}
    assert found["digest"] == [row["id"]]
    assert found["zsxq_title"] == [row["id"]]
    assert title_needs_ai("关于携程与Booking") is False


def test_reader_posts_and_disabled_sources_are_not_digested_or_recognized(conn):
    """读者帖、停用来源网站上不展示：夜间摘要和识图都不排它们。点名的文章照认，和星主帖共用的图照认。"""
    from researchlib.ocr import pending_images

    conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text, raw_meta)
        VALUES
          ('fangwei-zsxq', 'owner', 'o', '星主帖', '2026-10-29 12:00+08', '正文', '正文',
           '{"poster_name": "方伟Rey", "poster_role": "owner"}'),
          ('fangwei-zsxq', 'reader', 'r', '读者帖', '2026-10-29 13:00+08', '正文', '正文',
           '{"poster_name": "王小明", "poster_role": "guest"}'),
          ('chengfengpolang', 'paused', 'p', '停用来源', '2026-10-29 14:00+08', '正文', '正文', '{}')
        """
    )
    ids = {
        row["external_id"]: row["id"]
        for row in conn.execute("SELECT id, external_id FROM articles").fetchall()
    }
    images = [("owner", "11"), ("reader", "22"), ("reader", "33"), ("owner", "33"), ("paused", "44")]
    for external_id, digit in images:
        conn.execute(
            """
            INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
            SELECT id, source_id, %s, %s, %s FROM articles WHERE external_id = %s
            """,
            (f"{digit}.png", f"x/{digit}.png", digit * 32, external_id),
        )
    conn.execute("UPDATE sources SET enabled = false WHERE id = 'chengfengpolang'")
    conn.commit()

    digested = {row["id"] for row in digest_mod.pending_articles(conn, ["fangwei-zsxq", "chengfengpolang"], 10)}
    assert ids["owner"] in digested and ids["reader"] not in digested

    queued = {row["content_hash"] for row in pending_images(conn, 10)}
    assert queued == {"11" * 32, "33" * 32}
    named = {row["content_hash"] for row in pending_images(conn, 10, article_ids=[ids["reader"]])}
    assert named == {"22" * 32, "33" * 32}

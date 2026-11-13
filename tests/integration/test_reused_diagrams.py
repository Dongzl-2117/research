"""作者隔一阵才重用的示意图不是模板图：一段时间里篇篇都贴的才是（boilerplate._occasional）。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from researchlib.boilerplate import build_profile, clean_body, image_hashes
from researchlib.textutil import split_frontmatter

SHANGHAI = ZoneInfo("Asia/Shanghai")
START = datetime(2025, 1, 1, 9, 0, tzinfo=SHANGHAI)


def _article(conn, source_id, number, body):
    row = conn.execute(
        """
        INSERT INTO articles (source_id, external_id, title, published_at, body_md, body_text, content_hash)
        VALUES (%s, %s, %s, %s, %s, '', %s) RETURNING id
        """,
        (source_id, f"t/{number}.md", f"第 {number} 篇", START + timedelta(days=number), body, f"h{number}"),
    ).fetchone()
    return row["id"]


def _image(conn, article_id, source_id, ref, digest):
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (article_id, source_id, ref, f"{source_id}/{digest[:2]}/{digest}.png", digest),
    )


def test_reused_diagram_stays_and_every_post_banner_goes(conn):
    text = "这一段讲的是聚合平台怎么把供给变成商品，讲清楚了需求方为什么离不开它。"
    ids = []
    for number in range(120):
        # 每篇正文不一样，免得正文本身被当成多篇重复的模板句。
        lead, tail = f"第 {number} 篇开头：{text}", f"第 {number} 篇结尾：{text}"
        body = f"# 第 {number} 篇\n\n{lead}\n\n![](qr.png)\n\n{tail}\n"
        if number % 20 == 0:
            body += f"\n![A drawing of Aggregation Theory](diagram.png)\n\n第 {number} 篇的图后面：{text}\n"
        article_id = _article(conn, "stratechery", number, body)
        _image(conn, article_id, "stratechery", "qr.png", "b" * 64)
        if number % 20 == 0:
            _image(conn, article_id, "stratechery", "diagram.png", "d" * 64)
        ids.append(article_id)

    profile = build_profile(conn, "stratechery")
    # 二维码篇篇都贴；示意图 120 篇里 6 篇，隔 20 篇用一次。
    assert profile.images == {"b" * 64}

    body_md = conn.execute("SELECT body_md FROM articles WHERE id = %s", (ids[20],)).fetchone()["body_md"]
    _front, body = split_frontmatter(body_md)
    out = clean_body(body, profile, image_hashes(conn, ids[20]))
    assert "diagram.png" in out and "qr.png" not in out
    assert "第 20 篇开头" in out and "第 20 篇的图后面" in out

"""18902、15532 这种只有论坛导航的问答，不进首页和作者时间线。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib.empty_answer import backfill_empty_answers
from researchlib.ingest.duanyongping.reclassify import reclassify_source
from researchlib.web.app import create_app
from researchlib.web.queries import feed_counts


@pytest.fixture(autouse=True)
def _show_paused_sources(monkeypatch):
    """段永平在配置里 paused；这里测的是它自己的展示规则，照常展示。"""
    monkeypatch.setenv("RESEARCH_SHOW_PAUSED", "1")


SHANGHAI = ZoneInfo("Asia/Shanghai")

COOK = (
    "## 读者问\n\n"
    "Tim Cook wrote to employees when Apple crossed one trillion dollars. "
    "He said the work is not done. "
    "苹果市值到了一万亿美元，这封内部信把过程从头讲了一遍。"
    "读者把全文贴上来，想问段永平怎么看这家公司。\n\n"
    "## 段永平\n\n"
    "千里牛 2018-08-03 11:55 回本文开头 ↑\n"
)

PDD = """## 读者问

拼多多的增长很快，利润和补贴怎么看。对照 Meta 的广告，读者写了很长一篇。

## 段永平

李历 2018-08-05 23:48 回本文开头 ↑
"""

REAL = """## 读者问

茅台现在贵不贵？生意模式还在吗？

## 段永平

不贵。生意模式还在，我还拿着。
"""


def _insert(
    conn, article_id: int, external_id: str, title: str, body: str, when: datetime, meta: dict | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO articles (
          id, source_id, external_id, content_hash, title, published_at, body_md, body_text, raw_meta
        ) OVERRIDING SYSTEM VALUE
        VALUES (%s, 'duanyongping-blog', %s, %s, %s, %s, %s, %s, %s)
        """,
        (article_id, external_id, f"hash-{external_id}", title, when, body, body, Jsonb(meta or {})),
    )


def test_chrome_articles_18902_and_15532_leave_the_feed(conn, settings):
    _insert(conn, 18902, "dyp-18902", "库克万亿内部信", COOK, datetime(2018, 8, 3, 11, 55, tzinfo=SHANGHAI))
    _insert(conn, 15532, "dyp-15532", "拼多多到底能不能投", PDD, datetime(2018, 8, 5, 23, 48, tzinfo=SHANGHAI))
    _insert(conn, 15533, "dyp-real", "茅台现在还拿着", REAL, datetime(2018, 8, 6, 9, 0, tzinfo=SHANGHAI))
    apple = conn.execute("SELECT id FROM companies WHERE name_zh = '苹果'").fetchone()
    pdd = conn.execute("SELECT id FROM companies WHERE name_zh = '拼多多'").fetchone()
    conn.execute(
        "INSERT INTO article_mentions (article_id, company_id, depth, hit_count) VALUES (%s, %s, 'whole', 2)",
        (18902, apple["id"]),
    )
    conn.execute(
        "INSERT INTO article_mentions (article_id, company_id, depth, hit_count) VALUES (%s, %s, 'whole', 2)",
        (15532, pdd["id"]),
    )
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, heading, body) VALUES (%s, 0, '读者问', %s)",
        (18902, "苹果市值到了一万亿美元，这封内部信把过程从头讲了一遍。"),
    )
    conn.commit()

    hidden = backfill_empty_answers(conn)
    assert hidden == 2
    flags = {
        row["id"]: row["raw_meta"]
        for row in conn.execute(
            "SELECT id, raw_meta FROM articles WHERE id IN (18902, 15532, 15533) ORDER BY id"
        ).fetchall()
    }
    assert flags[18902]["suppressed"] == "empty_answer"
    assert flags[18902]["off_views"] is True
    assert flags[15532]["suppressed"] == "empty_answer"
    assert "suppressed" not in flags[15533]
    mentions = conn.execute(
        "SELECT count(*) AS n FROM article_mentions WHERE article_id IN (18902, 15532)"
    ).fetchone()
    assert mentions["n"] == 0

    counts = feed_counts(conn, ["duanyongping-blog"], include_off_timeline=False)
    assert counts["articles"] == 1

    # 回扫再跑一遍也不能把它们放回列表。
    report = reclassify_source(conn)
    assert report["counts"]["suppressed"] >= 2
    again = conn.execute("SELECT raw_meta FROM articles WHERE id = 18902").fetchone()
    assert again["raw_meta"]["suppressed"] == "empty_answer"

    app = create_app(settings)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "库克万亿内部信" not in home.text
        assert "拼多多到底能不能投" not in home.text
        assert "茅台现在还拿着" in home.text
        author = client.get("/authors/段永平")
        assert author.status_code == 200
        assert "库克万亿内部信" not in author.text
        assert "拼多多到底能不能投" not in author.text
        assert "茅台现在还拿着" in author.text
        found = client.get("/search", params={"q": "库克万亿内部信"})
        assert "/articles/18902" not in found.text
        assert "/articles/15532" not in found.text
        page = client.get("/articles/18902")
        assert page.status_code == 404
        preserved = conn.execute("SELECT body_md FROM articles WHERE id = 18902").fetchone()
        assert "一万亿美元" in preserved["body_md"]  # hidden does not delete the archive
        real = client.get("/articles/15533")
        assert "qa-ask" in real.text
        assert "生意模式还在" in real.text
        assert "无有效回答" not in real.text
        company = client.get(f"/companies/{apple['id']}")
        assert "库克万亿内部信" not in company.text


# ⽂ 是 U+2F42。这类拆出来的帖没有「## 段永平」，off_timeline 也挡不住作者页和检索。
KANGXI_SHELL = "尼古拉 ·特斯拉 2010-11-08 18:15 回本 ⽂开头 ↑"
PLAIN_SHELL = "千里牛 2010-11-08 18:15 回本文开头 ↑"


def test_backfill_hides_nav_shells_without_duan_heading(conn, settings):
    essay = "回到苹果的生意模式，我还拿着。开头那几年看不懂，后来利润起来了，就一直没卖。\n"
    _insert(
        conn,
        15079,
        "dyp-15079",
        KANGXI_SHELL,
        KANGXI_SHELL,
        datetime(2010, 11, 8, 18, 15, tzinfo=SHANGHAI),
        {"off_timeline": True},
    )
    _insert(
        conn,
        16416,
        "dyp-16416",
        PLAIN_SHELL,
        PLAIN_SHELL,
        datetime(2010, 11, 9, 18, 15, tzinfo=SHANGHAI),
        {"off_timeline": True},
    )
    _insert(
        conn,
        16417,
        "dyp-essay",
        "苹果的生意我还拿着",
        essay,
        datetime(2010, 11, 10, 9, 0, tzinfo=SHANGHAI),
        {"off_timeline": True},
    )
    conn.commit()

    hidden = backfill_empty_answers(conn)
    assert hidden == 2
    flags = {
        row["id"]: row["raw_meta"]
        for row in conn.execute("SELECT id, raw_meta FROM articles WHERE id IN (15079, 16416, 16417)").fetchall()
    }
    assert flags[15079]["suppressed"] == "empty_answer"
    assert flags[15079]["off_timeline"] is True
    assert flags[16416]["suppressed"] == "empty_answer"
    assert "suppressed" not in flags[16417]

    # 作者页收 off_timeline，但不收空回答。
    listed = feed_counts(conn, ["duanyongping-blog"], include_off_timeline=True)
    assert listed["articles"] == 1
    app = create_app(settings)
    with TestClient(app) as client:
        author = client.get("/authors/段永平")
        assert author.status_code == 200
        assert "尼古拉" not in author.text
        assert "回本文开头" not in author.text
        assert "苹果的生意我还拿着" in author.text
        found = client.get("/search", params={"q": "尼古拉"})
        assert "/articles/15079" not in found.text
        assert "/articles/16416" not in found.text


def test_backfill_suppresses_author_voice_all_and_keeps_a_real_judgment(conn, settings):
    essay = "今天天气不错，随便聊聊。\n"
    judgment = "## 读者问\n\n苹果现在贵不贵？\n\n## 段永平\n\n不贵。生意模式还在，我还拿着。\n"
    _insert(
        conn,
        41001,
        "dyp-all",
        "苹果还拿着",
        essay,
        datetime(2012, 6, 1, 9, 0, tzinfo=SHANGHAI),
        {"author_voice": "all"},
    )
    _insert(
        conn,
        41002,
        "dyp-judgment",
        "苹果贵不贵",
        judgment,
        datetime(2012, 6, 2, 9, 0, tzinfo=SHANGHAI),
        {"author_voice": "段永平"},
    )
    apple = conn.execute("SELECT id FROM companies WHERE name_zh = '苹果'").fetchone()
    conn.execute(
        "INSERT INTO article_mentions (article_id, company_id, depth, hit_count) VALUES (%s, %s, 'whole', 2)",
        (41001, apple["id"]),
    )
    conn.commit()

    hidden = backfill_empty_answers(conn)
    assert hidden == 1
    flags = {
        row["id"]: row["raw_meta"]
        for row in conn.execute("SELECT id, raw_meta FROM articles WHERE id IN (41001, 41002)").fetchall()
    }
    assert flags[41001]["suppressed"] == "no_judgment"
    assert flags[41001]["off_views"] is True
    assert flags[41001]["off_timeline"] is True
    assert "suppressed" not in flags[41002]
    mentions = conn.execute("SELECT count(*) AS n FROM article_mentions WHERE article_id = 41001").fetchone()
    assert mentions["n"] == 0
    assert backfill_empty_answers(conn) == 0

    listed = feed_counts(conn, ["duanyongping-blog"], include_off_timeline=True)
    assert listed["articles"] == 1
    app = create_app(settings)
    with TestClient(app) as client:
        author = client.get("/authors/段永平")
        assert "苹果还拿着" not in author.text
        assert "苹果贵不贵" in author.text
        found = client.get("/search", params={"q": "随便聊聊"})
        assert "/articles/41001" not in found.text
        page = client.get("/articles/41001")
        assert page.status_code == 404
        preserved = conn.execute("SELECT body_md FROM articles WHERE id = 41001").fetchone()
        assert "随便聊聊" in preserved["body_md"]

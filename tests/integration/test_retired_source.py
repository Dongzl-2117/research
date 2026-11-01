"""从 config/sources.yaml 删掉的来源：启动时停用，库里的文章留着，网站、接口和检索都不再出现。"""

from datetime import datetime

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app


def test_removed_source_leaves_the_site_but_keeps_rows(conn, settings):
    # 上线前的样子：来源还在库里、是启用的。sources 表不随每个测试清空，所以用 upsert。
    conn.execute(
        "INSERT INTO sources (id, name, author, kind, enabled)"
        " VALUES ('retired-blog', '旧博客', '王小明', 'markdown', true)"
        " ON CONFLICT (id) DO UPDATE SET enabled = true"
    )
    body = "苹果的生意模式还在，我还拿着。这家公司的利润是从用户的忠诚里来的。"
    article_id = conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text, raw_meta)
        VALUES ('retired-blog', 'old-1', 'hash-old-1', '苹果我还拿着', %s, %s, %s, %s)
        RETURNING id
        """,
        (datetime(2018, 8, 6, 9, 0, tzinfo=SHANGHAI), body, body, Jsonb({})),
    ).fetchone()["id"]
    apple = conn.execute("SELECT id FROM companies WHERE name_zh = '苹果'").fetchone()["id"]
    conn.execute(
        "INSERT INTO article_mentions (article_id, company_id, depth, hit_count) VALUES (%s, %s, 'whole', 2)",
        (article_id, apple),
    )
    conn.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get(f"/articles/{article_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{article_id}").status_code == 404
        assert client.get(f"/api/v1/articles/{article_id}.md").status_code == 404
        assert client.get("/?source=retired-blog").status_code == 404
        link = f"/articles/{article_id}"
        assert link not in client.get("/").text
        assert link not in client.get("/search", params={"q": "苹果我还拿着"}).text
        assert link not in client.get(f"/companies/{apple}").text

    enabled = conn.execute("SELECT enabled FROM sources WHERE id = 'retired-blog'").fetchone()["enabled"]
    assert enabled is False
    kept = conn.execute("SELECT count(*) AS n FROM articles WHERE source_id = 'retired-blog'").fetchone()["n"]
    assert kept == 1

from __future__ import annotations

import os
import time
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from researchlib.ingest.aihot import import_aihot
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ingest.wechat import main as wechat_main
from researchlib.seed import load_sample
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

ROOT = Path(__file__).resolve().parents[2]

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_sample_library_pages(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)
    depths = {
        row["depth"]: row["n"]
        for row in conn.execute(
            """
            SELECT m.depth, count(*) AS n
            FROM article_mentions m
            JOIN companies c ON c.id = m.company_id
            WHERE c.name_zh = '腾讯'
            GROUP BY m.depth
            """
        ).fetchall()
    }
    assert depths["title"] >= 1
    assert depths["section"] >= 1
    assert depths["passing"] >= 1

    app = create_app(settings)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "方伟公众号" in home.text
        assert "https://investment.zldong.com/" in home.text
        assert "示例数据" in home.text
        assert "research.zldong.com" not in home.text
        assert "按日期从新到旧" in home.text
        assert "按来源阅读全文" not in home.text
        assert home.text.find("1月15日") < home.text.find("9月9日")
        assert home.text.find("晚间短记") < home.text.find("晨间短记") < home.text.find("年初备忘")
        assert home.text.find("21:40") < home.text.find("09:05") < home.text.find("08:30")
        filtered = client.get("/", params={"source": "chengfengpolang"})
        assert "亚马逊云和广告" in filtered.text
        assert "晚间短记" not in filtered.text

        authors = client.get("/authors")
        assert authors.status_code == 200
        assert "按作者查看" in authors.text
        assert "方伟" in authors.text
        assert "乘风破浪" in authors.text
        assert "林文丰" not in authors.text
        author = client.get("/authors/方伟")
        assert author.status_code == 200
        assert "晚间短记" in author.text
        assert "亚马逊云和广告" not in author.text
        assert client.get("/authors/林文丰").status_code == 404

        search = client.get("/search", params={"q": "腾讯"})
        assert search.status_code == 200
        assert "<mark>腾讯</mark>" in search.text
        assert "方伟</span><strong>3</strong>" in search.text
        assert "乘风破浪</span><strong>0</strong>" in search.text
        assert 'class="pills search-pills"' in search.text
        by_author = client.get("/search", params={"q": "方伟"})
        assert "晚间短记" in by_author.text
        assert "亚马逊云和广告" not in by_author.text
        assert "没有找到" in client.get("/search", params={"q": "绝无此词xyz"}).text

        companies = client.get("/companies")
        assert "按公司查看" in companies.text
        assert "腾讯" in companies.text
        assert "贵州茅台" in companies.text
        company_id = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
        company = client.get(f"/companies/{company_id}")
        assert "标题" in company.text
        assert "单节" in company.text
        assert "一带而过" in company.text
        assert "按月有提及的文章数" in company.text

        article_id = conn.execute(
            "SELECT id FROM articles WHERE external_id = 'sample:battery-note'"
        ).fetchone()["id"]
        article = client.get(f"/articles/{article_id}")
        assert article.status_code == 200
        assert "宁德时代" in article.text
        assert "/media/fangwei-mp/" in article.text
        assert 'rel="canonical" href="https://investment.zldong.com/articles/' in article.text
        image = article.text.split('src="', 1)[1].split('"', 1)[0]
        media = client.get(image)
        assert media.status_code == 200
        assert media.headers["content-type"].startswith("image/png")
        assert client.get("/media/fangwei-mp/../../etc/passwd").status_code == 404
        assert client.get("/?source=gudujinhuati").status_code == 404
        assert client.get("/health").json() == {"status": "ok"}
        assert "Disallow" in client.get("/robots.txt").text


def test_wechat_ingest_is_idempotent(monkeypatch, tmp_path, database_url, conn):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))
    archive = tmp_path / "inbox"
    archive.mkdir()
    body = "# 腾讯示例导入\n\n这里讨论腾讯的广告。\n\n![](pic.png)\n"
    with zipfile.ZipFile(archive / "01-text.zip", "w") as bundle:
        bundle.writestr("2024-08-08-note.md", body)
    with zipfile.ZipFile(archive / "02-images-a.zip", "w") as bundle:
        bundle.writestr("pic.png", PNG)

    assert wechat_main(["--source", "fangwei-mp", "--input", str(archive)]) == 0
    conn.rollback()
    count = conn.execute("SELECT count(*) AS n FROM articles WHERE source_id = 'fangwei-mp'").fetchone()["n"]
    assert count == 1
    assert wechat_main(["--source", "fangwei-mp", "--input", str(archive)]) == 0
    conn.rollback()
    count = conn.execute("SELECT count(*) AS n FROM articles WHERE source_id = 'fangwei-mp'").fetchone()["n"]
    assert count == 1
    with zipfile.ZipFile(archive / "01-text.zip", "w") as bundle:
        bundle.writestr("2024-08-08-note.md", body.replace("广告", "广告与游戏"))
    with zipfile.ZipFile(archive / "02-images-a.zip", "w") as bundle:
        bundle.writestr("pic.png", PNG)
    assert wechat_main(["--source", "fangwei-mp", "--input", str(archive)]) == 0
    conn.rollback()
    row = conn.execute(
        "SELECT title, body_md FROM articles WHERE source_id = 'fangwei-mp'"
    ).fetchone()
    assert "游戏" in row["body_md"]


def test_aihot_import_filters_whitelist(conn, settings, tmp_path):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aihot_articles (
          id bigint PRIMARY KEY,
          title text,
          content text,
          source text,
          published_at timestamptz
        )
        """
    )
    conn.execute("DELETE FROM aihot_articles")
    conn.execute(
        """
        INSERT INTO aihot_articles (id, title, content, source, published_at) VALUES
          (1, '腾讯备忘', '这是从别处读来的腾讯笔记', 'fangwei-mp', '2024-01-02'),
          (2, '停用源', '孤独进化体的正文', 'gudujinhuati', '2024-01-03'),
          (3, '未知', '不属于白名单', 'random-blog', '2024-01-04')
        """
    )
    conn.commit()
    spec = tmp_path / "map.yaml"
    spec.write_text(
        """
table: aihot_articles
columns:
  external_id: [id]
  title: [title]
  body: [content]
  source: [source]
  published_at: [published_at]
source_map: {}
""",
        encoding="utf-8",
    )
    imported = settings.__class__.from_env(
        {
            "DATABASE_URL": settings.database_url,
            "DATA_DIR": str(settings.data_dir),
            "PUBLIC_BASE_URL": settings.public_base_url,
            "SOURCES_FILE": str(settings.sources_path),
            "AIHOT_MAP_FILE": str(spec),
            "AIHOT_DATABASE_URL": settings.database_url,
            "BIND_HOST": "127.0.0.1",
            "BIND_PORT": "8088",
        }
    )
    report = import_aihot(conn, imported)
    assert report.inserted == 1
    assert report.rejected == 2
    again = import_aihot(conn, imported)
    assert again.inserted == 0
    assert again.unchanged == 1
    assert again.rejected == 2
    row = conn.execute("SELECT external_id FROM articles WHERE source_id = 'fangwei-mp'").fetchone()
    assert row["external_id"] == "aihot:1"
    assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 1


def test_index_batch_stays_fast(conn, settings):
    source = next(source for source in load_sources(settings.sources_path) if source.id == "fangwei-mp")
    docs = []
    for index in range(40):
        docs.append(
            ParsedDoc(
                external_id=f"speed-{index}",
                title=f"速度 {index}",
                published_at=datetime(2024, 1, 1, tzinfo=SHANGHAI),
                body_md=f"第 {index} 篇提到腾讯和贵州茅台，用来测索引。",
                raw_meta={"format": "markdown"},
                image_refs=[],
            )
        )
    started = time.perf_counter()
    report = store_documents(
        conn,
        source=source,
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )
    elapsed = time.perf_counter() - started
    assert report.inserted == 40
    assert report.errors == []
    assert elapsed < 30
    hit = conn.execute(
        """
        SELECT count(*) AS n
        FROM articles, plainto_tsquery('chinese', '茅台') q
        WHERE search_tsv @@ q AND external_id LIKE 'speed-%'
        """
    ).fetchone()
    assert hit["n"] == 40

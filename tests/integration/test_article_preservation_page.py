"""真实文章路由上的合成保真回归，不排模型任务、不读取业务资料。"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from PIL import Image

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.mdlinks import finish_html, prepare_body_html
from researchlib.sources import load_sources
from researchlib.translate.segments import splice_segments, translatable_blocks
from researchlib.web.app import create_app


def test_article_bilingual_original_and_chinese_retain_nontranslatable_content(conn, settings, tmp_path, monkeypatch):
    monkeypatch.setattr("researchlib.web.routes.schedule_digests", lambda *_args, **_kwargs: None)
    source = next(item for item in load_sources(settings.sources_path) if item.id == "stratechery")
    image = tmp_path / "synthetic.png"
    Image.new("RGB", (32, 32), "white").save(image)
    body = (
        "# Synthetic report\n\n## Revenue\n\nBefore the table.\n\n"
        "| Year | Revenue |\n| --- | --- |\n| 2026 | 123.45% |\n\n"
        "![Revenue](synthetic.png)\n\n```python\nreturn 42 < 100\n```\n\n"
        "## Conclusion\n\nAfter the table."
    )
    store_documents(
        conn, source=source,
        docs=[ParsedDoc("synthetic-preservation", "Synthetic report", datetime(2026, 1, 1, tzinfo=UTC),
                        body, {"language": "en"}, [(image.name, image)])],
        data_dir=settings.data_dir, archive_files=1, images_in_archive=1, errors=[],
    )
    article = conn.execute(
        "SELECT id, content_hash FROM articles WHERE external_id = 'synthetic-preservation'"
    ).fetchone()
    images = conn.execute("SELECT original_ref, storage_path FROM article_images WHERE article_id = %s",
                          (article["id"],)).fetchall()
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    en = prepare_body_html(body, "Synthetic report", mapping, None, None)
    zh = finish_html(splice_segments(en, {index: f"<p>合成译文第{index}段。</p>"
                                          for index, _html in translatable_blocks(en)}), None)
    conn.execute(
        "INSERT INTO article_translations"
        " (article_id, content_hash, title_zh, body_html, body_text, complete, segments)"
        " VALUES (%s, %s, '合成报告', %s, '合成译文', true, 6)",
        (article["id"], article["content_hash"], zh),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        for suffix in ("", "/original", "/zh"):
            response = client.get(f"/articles/{article['id']}{suffix}")
            assert response.status_code == 200
            page = response.text
            for content in ("<table", "<td>2026</td>", "123.45%", "<pre", "return 42 &lt; 100",
                            f'src="{mapping[image.name]}"'):
                assert content in page
            assert 'src="/static/article.js?v=' in page
    assert conn.execute("SELECT body_md FROM articles WHERE id = %s", (article["id"],)).fetchone()["body_md"] == body

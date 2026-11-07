"""Stratechery 2026 年网页导出：单看原文的页面和 .md 接口拼回段落，对照页照旧按译制时的段配对。"""

import dataclasses
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate.segments import iter_segments
from researchlib.translate.service import _render_original
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
JOINED = "I have pointed to this chart before, mostly to note how few people read the footnote under it."
# 编的样本，形状照线上的网页导出：一个文本节点一行，链接字单独一行。
EXPORT = (
    "# Widget Day\n"
    "- source: member-rss\n\n"
    "Widget Margins\n"
    "I have pointed to this chart\n"
    "before\n"
    ", mostly to note how few people read the footnote under it.\n"
    "The deal closed in\n"
    "March\n"
    ". After that, the regulators moved on and the widget market went back to normal.\n"
    "JD:\n"
    "Thanks for having me, the widget market is bigger than people think.\n"
)


def _store(conn, settings) -> int:
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    # 编的短样本，不测 stratechery 的 min_chars 门槛。
    strat = dataclasses.replace(sources["stratechery"], min_chars=0)
    store_documents(
        conn,
        source=strat,
        docs=[
            ParsedDoc(
                external_id="2026/widget-day",
                title="Widget Day",
                published_at=datetime(2026, 9, 1, 8, 0, tzinfo=SHANGHAI),
                body_md=EXPORT,
                raw_meta={"language": "en"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    return conn.execute("SELECT id FROM articles WHERE external_id = '2026/widget-day'").fetchone()["id"]


def _prose(page: str) -> str:
    return page.split('<div class="prose', 1)[1].split("</div>", 1)[0]


def test_original_page_and_md_join_the_split_lines(conn, settings):
    article_id = _store(conn, settings)
    with TestClient(create_app(settings)) as client:
        prose = _prose(client.get(f"/articles/{article_id}").text)
        md = client.get(f"/api/v1/articles/{article_id}.md").text
    for shown in (prose, md):
        assert JOINED in shown
        assert "The deal closed in March. After that, the regulators moved on" in shown
        assert "JD: Thanks for having me" in shown
    # 小标题还是单独一段，句子不再被链接字拆成几行。
    assert re.search(r"<p[^>]*>Widget Margins</p>", prose)
    assert "chart<br" not in prose and "\nbefore\n" not in md
    # 库里的原文和检索文字不改。
    row = conn.execute("SELECT body_md FROM articles WHERE id = %s", (article_id,)).fetchone()
    assert "this chart\nbefore\n, mostly" in row["body_md"]


def test_bilingual_view_still_pairs_the_translated_segments(conn, settings):
    article_id = _store(conn, settings)
    article = dict(conn.execute("SELECT * FROM articles WHERE id = %s", (article_id,)).fetchone())
    # 译制送的就是这一份（没拼的）原文：译文一段对一个叶子。
    source_html, content_hash = _render_original(conn, article, settings.data_dir)
    leaves = list(iter_segments(source_html))
    names = "甲乙丙丁戊己庚辛壬癸"
    assert 0 < len(leaves) <= len(names)
    zh_html = "".join(f"<p>第{names[index]}段的译文。</p>" for index in range(len(leaves)))
    conn.execute(
        """
        INSERT INTO article_translations (article_id, content_hash, title_zh, body_html, body_text, complete, segments)
        VALUES (%s, %s, '小部件日', %s, %s, true, %s)
        """,
        (article_id, content_hash, zh_html, zh_html, len(leaves)),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        pairs = client.get(f"/articles/{article_id}").text
        original = _prose(client.get(f"/articles/{article_id}/original").text)
    # 对照照旧按译制时的段一对一，拼了段落就会比译文少一段或多一段、整页分开。
    assert "对不上" not in pairs
    assert pairs.count('class="prose bilingual-en') == pairs.count('class="prose bilingual-zh') == len(leaves)
    # 点「原文」只看英文：拼回段落。
    assert JOINED in original and re.search(r"<p[^>]*>Widget Margins</p>", original)

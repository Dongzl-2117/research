import html
import re
from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import coverage_recent, source_stats
from researchlib.web.routes import _coverage_rows


def _visible_text(markup):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", markup)).split())


def _row(page, source):
    return _visible_text(page.split(f'id="coverage-{source}"', 1)[1].split("</li>", 1)[0])


def test_counts_use_source_publication_time_and_visible_enabled_originals(conn, settings):
    sources = {s.id: s for s in load_sources(settings.sources_path)}
    target = sources["fangwei-mp"]
    dated = datetime(2024, 3, 31, 8, tzinfo=UTC)
    for source, key, published, text in [
        (target, "a", dated, "护城河来自客户长期保留和供应效率。"),
        (target, "b", None, "这份没有日期的护城河记录讨论客户需求。"),
        (target, "hide", dated, "这是应隐藏的经营数据。"),
        (sources["gudujinhuati"], "c", None, "护城河取决于业务模型能否持续创造价值。"),
    ]:
        doc = ParsedDoc(key, key + " 研究资料", published, text, {}, [])
        store_documents(conn, source=source, docs=[doc], data_dir=settings.data_dir,
                        archive_files=1, images_in_archive=0, errors=[])
    conn.execute("UPDATE articles SET raw_meta = raw_meta || '{\"hidden\": \"short_post\"}' WHERE external_id = 'hide'")
    conn.execute("INSERT INTO articles(source_id, external_id, content_hash, title, body_md, body_text, published_at)"
                 " VALUES ('linwenfeng-mp','off','off','已停用的护城河来源','','护城河',now())")
    conn.execute(
        "INSERT INTO articles(source_id, external_id, content_hash, title, body_md, body_text,"
        " published_at, duplicate_of)"
        " SELECT source_id, 'dup', 'dup', '重复转载', '', '护城河', now(), id FROM articles WHERE external_id = 'a'"
    )
    conn.commit()
    stats = source_stats(conn, [target.id])
    assert stats[target.id]["n"] == 2 and stats[target.id]["undated"] == 1
    assert [row["title"] for row in coverage_recent(conn, [target.id])[target.id]] == ["a 研究资料"]
    with TestClient(create_app(settings)) as client:
        authors = client.get("/authors").text
        assert "2 篇，含日期不详 1 篇" in _row(authors, target.id)
        assert "2024-03-31" in _row(authors, target.id) and "a 研究资料" in _row(authors, target.id)
        unknown = _row(authors, "gudujinhuati")
        assert "1 篇，含日期不详 1 篇" in unknown and "最新有日期的文章：未知" in unknown
        assert "c 研究资料" not in authors
        assert "coverage-linwenfeng-mp" not in authors and "手动导入归档" in authors
        scoped = client.get("/authors/方伟", params={"source": target.id}).text
        assert f'id="coverage-{target.id}"' in scoped and 'id="coverage-fangwei-zsxq"' not in scoped
        search = client.get("/search", params={"q": "护城河", "source": target.id}).text
        assert "来源数字按匹配的文章篇数统计" in search
        assert "全部来源 3" in _visible_text(search) and "方伟公众号 2" in _visible_text(search)
        assert "当前候选列表共 2 处片段，来自 2 篇" in _visible_text(search)
        assert f'/authors#coverage-{target.id}' in search


def test_overdue_depends_on_observed_crawl_and_sufficient_dated_history(conn, settings):
    source = next(s for s in load_sources(settings.sources_path) if s.id == "gudujinhuati")
    conn.execute(
        """INSERT INTO articles (source_id, external_id, content_hash, title, body_md, body_text, published_at)
           SELECT %s, 'gap-' || i, 'gap-' || i, '研究资料', '', '', now() - (i + 15) * interval '1 day'
           FROM generate_series(0, 9) i""", (source.id,),
    )
    stats = source_stats(conn, [source.id])
    manual = _coverage_rows(conn, [replace(source, collection="")], stats, "UTC")[0]
    assert manual["collection"] == "未知" and manual["overdue"] is None
    conn.execute(
        """INSERT INTO app_state(key, value) VALUES (%s, '{"origin":"crawl"}')
           ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()""",
        ("import:" + source.id,),
    )
    row = _coverage_rows(conn, [source], stats, "UTC")[0]
    assert row["overdue"] == 15
    conn.execute("DELETE FROM articles WHERE external_id = 'gap-9'")
    assert _coverage_rows(conn, [source], stats, "UTC")[0]["overdue"] is None

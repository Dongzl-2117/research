"""没日期的段永平条目不进首页按天，作者页和检索还在。"""

from pathlib import Path

from fastapi.testclient import TestClient

from researchlib.ingest.duanyongping.emit import render_markdown
from researchlib.ingest.duanyongping.extract import lines_to_blocks, parse_xml
from researchlib.ingest.duanyongping.segment import entries_from_blocks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.parse import parse_archive
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import feed_counts

FIXTURE = Path(__file__).resolve().parents[2] / "scripts" / "duanyongping" / "fixtures" / "sample.xml"


def _source(settings, source_id):
    return next(source for source in load_sources(settings.sources_path) if source.id == source_id)


def test_undated_entries_skip_the_home_feed(conn, settings, tmp_path):
    raw = FIXTURE.read_text(encoding="utf-8")
    entries, _mode, _posts = entries_from_blocks(lines_to_blocks(parse_xml(raw)))
    kept = [entry for entry in entries if entry.keep]
    for entry in kept:
        (tmp_path / f"{entry.external_id}.md").write_text(render_markdown(entry), encoding="utf-8")
    docs, errors, files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == len(kept)
    source = _source(settings, "duanyongping-blog")
    store_documents(
        conn,
        source=source,
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=files,
        images_in_archive=0,
        errors=[],
    )
    fangwei = _source(settings, "fangwei-mp")
    store_documents(
        conn,
        source=fangwei,
        docs=[
            ParsedDoc(
                external_id="fw-undated",
                title="方伟这篇没有日期",
                published_at=None,
                body_md="方伟这篇没有日期，仍然要出现在首页的日期不详里。\n",
                raw_meta={"format": "markdown"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    ids = [source.id]
    hidden = feed_counts(conn, ids, include_off_timeline=False)
    shown = feed_counts(conn, ids, include_off_timeline=True)
    assert hidden["undated"] == 0
    assert shown["undated"] == 1
    assert hidden["articles"] == shown["articles"] - 1

    app = create_app(settings)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "贵不贵" in home.text
        assert "护城河是什么" not in home.text
        undated_page = client.get("/", params={"page": 2})
        assert "方伟这篇没有日期" in undated_page.text
        assert "护城河是什么" not in undated_page.text
        author = client.get("/authors/段永平", params={"page": 2})
        assert author.status_code == 200
        assert "护城河是什么" in author.text
        found = client.get("/search", params={"q": "护城河是什么"})
        assert found.status_code == 200
        assert "护城河是什么" in found.text

    row = conn.execute(
        """
        SELECT c.name_zh, m.depth
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        JOIN companies c ON c.id = m.company_id
        WHERE a.source_id = 'duanyongping-blog' AND a.title LIKE '%贵不贵%'
        """
    ).fetchone()
    assert row["name_zh"] == "贵州茅台"
    assert row["depth"] == "title"

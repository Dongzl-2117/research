"""Stratechery 补钟点：只有日期的篇按官网时刻改、哈希对齐、页面显示钟点；同一份归档重导不退回零点。"""

from datetime import UTC, date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from researchlib import stratechery_times
from researchlib.ingest.parse import parse_archive
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI, content_hash
from researchlib.web.app import create_app


def _write(root: Path, slug: str, title: str, day: str) -> None:
    # 句子各不相同，三篇不会被认成同一篇；够长，不会被 5500 字门槛标成短帖。
    body = "".join(
        f"Point {index} about {title} is that the market for item {index} keeps growing every quarter. "
        for index in range(120)
    )
    path = root / day[:4] / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{title}"\npublished: "{day}"\n---\n\n# {title}\n\n{body}\n', encoding="utf-8")


def _import(conn, settings, archive: Path, work: Path):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "stratechery")
    work.mkdir(parents=True, exist_ok=True)
    docs, errors, files, images = parse_archive(archive, work, id_mode="path")
    return store_documents(
        conn,
        source=source,
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=files,
        images_in_archive=images,
        errors=errors,
    )


def _row(conn, external_id: str):
    return conn.execute(
        """
        SELECT id, title, body_md, published_at, content_hash, raw_meta
        FROM articles WHERE source_id = 'stratechery' AND external_id = %s
        """,
        (external_id,),
    ).fetchone()


POSTS = [
    {"slug": "widget-margins", "title": {"rendered": "Widget Margins"},
     "date": "2025-09-22T06:00:00", "date_gmt": "2025-09-22T10:00:00"},
    # 美国晚上发的：上海是第二天早上。
    {"slug": "gadget-day", "title": {"rendered": "Gadget Day"},
     "date": "2025-09-23T21:30:00", "date_gmt": "2025-09-24T01:30:00"},
]


def test_filled_clock_shows_on_pages_and_survives_reimport(conn, settings, tmp_path, monkeypatch, capsys):
    archive = tmp_path / "inbox"
    _write(archive, "widget-margins", "Widget Margins", "2025-09-22")
    _write(archive, "gadget-day", "Gadget Day", "2025-09-23")
    _write(archive, "sprocket-notes", "Sprocket Notes", "2025-09-24")  # 官网接口里没有
    assert _import(conn, settings, archive, tmp_path / "work1").inserted == 3
    widget = _row(conn, "2025/widget-margins.md")
    assert widget["published_at"] == datetime(2025, 9, 22, tzinfo=SHANGHAI)
    assert widget["raw_meta"]["time_known"] is False
    conn.execute(
        "INSERT INTO article_digests (article_id, content_hash, model, summary) VALUES (%s, %s, 'synthetic', '小结')",
        (widget["id"], widget["content_hash"]),
    )
    conn.commit()

    asked = []

    def fake_fetch(first, last):
        asked.append((first, last))
        return POSTS

    monkeypatch.setattr(stratechery_times, "fetch_posts", fake_fetch)
    monkeypatch.setattr(stratechery_times.Settings, "from_env", lambda *args, **kwargs: settings)

    assert stratechery_times.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "更正发布时间: 2" in out and "对上 2 篇，没对上 1 篇" in out
    assert _row(conn, "2025/widget-margins.md")["published_at"] == widget["published_at"]

    assert stratechery_times.main([]) == 0
    assert asked[-1] == (date(2025, 9, 22), date(2025, 9, 24))
    fixed = _row(conn, "2025/widget-margins.md")
    assert fixed["published_at"] == datetime(2025, 9, 22, 10, 0, tzinfo=UTC)
    assert fixed["raw_meta"]["time_known"] is True
    assert fixed["raw_meta"]["date_source"] == "wordpress"
    assert fixed["raw_meta"]["calendar_day"] == "2025-09-22"
    assert fixed["content_hash"] == content_hash("Widget Margins", fixed["body_md"], fixed["published_at"])
    digest = conn.execute("SELECT content_hash FROM article_digests WHERE article_id = %s", (widget["id"],))
    assert digest.fetchone()["content_hash"] == fixed["content_hash"]
    gadget = _row(conn, "2025/gadget-day.md")
    assert gadget["published_at"].astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2025-09-24 09:30"
    assert gadget["raw_meta"]["calendar_day"] == "2025-09-23"
    notes = _row(conn, "2025/sprocket-notes.md")
    assert notes["published_at"] == datetime(2025, 9, 24, tzinfo=SHANGHAI)
    assert notes["raw_meta"]["time_known"] is False
    conn.commit()

    with TestClient(create_app(settings)) as client:
        feed = client.get("/?source=stratechery").text
        assert ">18:00</time>" in feed and ">09:30</time>" in feed
        assert feed.count(">—</time>") == 1
        page = client.get(f"/articles/{widget['id']}").text
        assert "2025-09-22 18:00</time>" in page

    # 同一份只有日期的归档再导一次：钟点留着，三篇都算没改。
    report = _import(conn, settings, archive, tmp_path / "work2")
    assert (report.unchanged, report.updated) == (3, 0)
    again = _row(conn, "2025/widget-margins.md")
    assert again["published_at"] == fixed["published_at"]
    assert again["content_hash"] == fixed["content_hash"]
    assert again["raw_meta"]["time_known"] is True
    conn.commit()

    # 第二晚：只剩官网上没有的那篇，只问它那一天。
    assert stratechery_times.main([]) == 0
    assert asked[-1] == (date(2025, 9, 24), date(2025, 9, 24))
    assert "对上 0 篇，没对上 1 篇" in capsys.readouterr().out


def test_reimport_with_a_new_day_takes_the_archive_date(conn, settings, tmp_path, monkeypatch):
    """归档换了日期（以前抓错了日子又改回来）：照归档的来，夜里再按新日子补钟点。"""
    archive = tmp_path / "inbox"
    _write(archive, "widget-margins", "Widget Margins", "2025-09-22")
    _import(conn, settings, archive, tmp_path / "work1")
    conn.commit()
    monkeypatch.setattr(stratechery_times, "fetch_posts", lambda first, last: POSTS)
    monkeypatch.setattr(stratechery_times.Settings, "from_env", lambda *args, **kwargs: settings)
    assert stratechery_times.main([]) == 0
    _write(archive, "widget-margins", "Widget Margins", "2025-09-29")
    report = _import(conn, settings, archive, tmp_path / "work2")
    assert report.updated == 1
    moved = _row(conn, "2025/widget-margins.md")
    assert moved["published_at"] == datetime(2025, 9, 29, tzinfo=SHANGHAI)
    assert moved["raw_meta"]["time_known"] is False
    assert "calendar_day" not in moved["raw_meta"]

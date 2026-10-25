"""定时增量导入：AIHOT 只读最近几天、只读白名单来源；定时导入的来源停更明显超过平时间隔时，首页提示。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from researchlib.ingest.aihot import import_aihot, source_values
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app


def _aihot(conn, settings, tmp_path, rows):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aihot_recent (
          id bigint PRIMARY KEY, title text, content text, source text, published_at timestamptz
        )
        """
    )
    conn.execute("DELETE FROM aihot_recent")
    for row in rows:
        conn.execute("INSERT INTO aihot_recent VALUES (%s, %s, %s, %s, %s)", row)
    conn.commit()
    spec = tmp_path / "map.yaml"
    spec.write_text(
        "table: aihot_recent\ncolumns:\n  external_id: [id]\n  title: [title]\n  body: [content]\n"
        "  source: [source]\n  published_at: [published_at]\nsource_map:\n  mp-gudujinhua: gudujinhuati\n",
        encoding="utf-8",
    )
    return settings.__class__.from_env(
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


def test_incremental_import_reads_recent_whitelisted_rows(conn, settings, tmp_path):
    now = datetime.now(UTC)
    imported = _aihot(
        conn,
        settings,
        tmp_path,
        [
            (1, "新的", "孤独进化体最近写了一篇讲泡泡玛特的文章，谈的是 IP 的生命周期。", "mp-gudujinhua",
             now - timedelta(days=2)),
            (2, "旧的", "孤独进化体很早以前写的一篇，讲的是农夫山泉的渠道。", "mp-gudujinhua",
             now - timedelta(days=90)),
            (3, "别的源", "不在白名单里的源。", "lin-ke", now - timedelta(days=1)),
        ],
    )
    report = import_aihot(conn, imported, days=14)
    assert report.inserted == 1 and report.rejected == 1
    titles = [row["title"] for row in conn.execute("SELECT title FROM articles").fetchall()]
    assert titles == ["新的"]
    full = import_aihot(conn, imported)
    assert full.inserted == 1 and full.unchanged == 1
    recorded = conn.execute("SELECT value FROM app_state WHERE key = 'import:gudujinhuati'").fetchone()
    assert recorded is not None and '"inserted"' in recorded["value"]


def test_source_values_only_cover_enabled_sources(settings):
    sources = load_sources(settings.sources_path)
    values = source_values(sources, {"mp-gudujinhua": "gudujinhuati", "lin-mp": "linwenfeng-mp"})
    assert "mp-gudujinhua" in values and "fangwei-mp" in values
    # 林文丰公众号停用，不进；Stratechery 已经启用（#61），照常进。
    assert "lin-mp" not in values and "linwenfeng-mp" not in values and "stratechery" in values


def test_home_shows_a_notice_for_a_stalled_source(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    start = datetime.now(UTC) - timedelta(days=60)
    docs = [
        ParsedDoc(
            external_id=f"mp/{index}",
            title=f"第 {index} 篇",
            published_at=start + timedelta(days=2 * index),
            body_md=f"第 {index} 篇写的是腾讯第 {index} 个季度的广告收入和视频号的库存，和别的篇不一样。",
            raw_meta={},
            image_refs=[],
        )
        for index in range(12)
    ]

    def store(origin):
        store_documents(conn, source=sources["fangwei-mp"], docs=docs, data_dir=settings.data_dir,
                        archive_files=len(docs), images_in_archive=0, errors=[], origin=origin)

    # 手动导入的归档本来就是旧的：不提示。
    store("archive")
    with TestClient(create_app(settings)) as client:
        assert "没有新文章" not in client.get("/").text
    # 定时任务从 AIHOT 导入，但最近一篇已经很久了：提示。
    store("aihot")
    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        assert "方伟公众号 37 天没有新文章" in home or "方伟公众号 38 天没有新文章" in home
        assert "最近一次导入" in home
    with TestClient(create_app(replace(settings, stale_banner_enabled=False))) as client:
        assert "没有新文章" not in client.get("/").text

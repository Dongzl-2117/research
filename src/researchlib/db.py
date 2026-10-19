"""Postgres 连接。每条连接都带上同一套 zhparser 参数，避免索引和查询分词不一致。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from researchlib.chunks import backfill_missing_chunks
from researchlib.companies import seed_companies
from researchlib.config import Settings
from researchlib.migrate import migrate
from researchlib.sources import load_sources, sync_sources

SEARCH_SETTINGS = (
    "SET zhparser.punctuation_ignore = on",
    "SET zhparser.seg_with_duality = on",
    "SET zhparser.multi_short = on",
)


def connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def ensure_search_session(conn: psycopg.Connection) -> None:
    row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'zhparser'").fetchone()
    if row is None:
        return
    for statement in SEARCH_SETTINGS:
        conn.execute(statement)


def prepare(settings: Settings) -> psycopg.Connection:
    """迁移、同步白名单和公司种子。调用方负责关闭连接。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    try:
        migrate(conn)
        ensure_search_session(conn)
        sync_sources(conn, load_sources(settings.sources_path))
        seed_companies(conn)
        conn.commit()
        # 003_retrieval 之前入库的文章没有段落块。只补缺的，已有的不动。
        # 不在这里跑摘要；全库摘要只由 research-digest 手动执行。
        backfill_missing_chunks(conn)
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


@contextmanager
def session(settings: Settings) -> Iterator[psycopg.Connection]:
    conn = prepare(settings)
    try:
        yield conn
    finally:
        conn.close()

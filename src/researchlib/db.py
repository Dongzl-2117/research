"""Postgres 连接。每条连接都带上同一套 zhparser 参数，避免索引和查询分词不一致。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from researchlib import embeddings
from researchlib.chunks import backfill_missing_chunks
from researchlib.companies import seed_companies
from researchlib.config import Settings
from researchlib.migrate import migrate
from researchlib.posters import refresh_poster_roles
from researchlib.sources import load_sources, sync_sources
from researchlib.visibility import refresh_length_floors

SEARCH_SETTINGS = (
    "SET zhparser.punctuation_ignore = on",
    "SET zhparser.seg_with_duality = on",
    "SET zhparser.multi_short = on",
)


# 应用自己的连接带两个设置，psql、备份不受影响：
# - 关掉 JIT：估价过了十万（召回段落、按意思找的几条 SQL 常常过），Postgres 先花一两百毫秒把表达式编译成机器码，
#   再省下的执行时间远不到这么多。这个库的查询都是一次几毫秒到几百毫秒的小查询，用不上。
# - 顺序扫描每次从表头开始（synchronize_seqscans=off）。默认开着时，大表（段落块表三百多 MB）的顺序扫描从上一次
#   停下的地方接着扫。一路全文召回先取命中的前 FTS_POOL 块，没有排序：命中超过这么多块、计划又是顺序扫描时，
#   同一个词连搜两次进池子的块不一样，结果跟着变（迁移 036 补了统计以后，「苹果 服务收入」这类词会走到这种计划）。
CONNECT_OPTIONS = "-c jit=off -c synchronize_seqscans=off"


def connect(settings: Settings) -> psycopg.Connection:
    embeddings.configure(settings)
    return psycopg.connect(settings.database_url, row_factory=dict_row, options=CONNECT_OPTIONS)


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
        sources = load_sources(settings.sources_path)
        sync_sources(conn, sources)
        seed_companies(conn)
        # 星主的星球昵称（poster_names）改了，按新名单重算已入库帖子的原创标记，不用重新导入。
        refresh_poster_roles(conn, sources)
        # 来源的短帖门槛（min_chars）改了，按新门槛重标已入库的短帖。
        refresh_length_floors(conn, sources)
        conn.commit()
        # 装了 pgvector 才建向量表；没装就只走关键词检索。
        embeddings.ensure_schema(conn)
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

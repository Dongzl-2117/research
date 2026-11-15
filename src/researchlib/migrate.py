"""按文件名顺序执行迁移。语句以行尾分号切开，美元引号里的分号不切开。"""

from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in script.splitlines():
        stripped = line.strip()
        if not in_dollar and (not stripped or stripped.startswith("--")):
            continue
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and stripped.endswith(";"):
            statement = "\n".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# 迁移的互斥锁（pg_advisory_lock 的键）。新容器启动的同时，宿主机的定时任务、补跑循环也会 prepare：
# 不锁的话两边都看到新迁移没跑，各跑一遍，后提交的撞主键退出（输的要是网页进程，这次启动就失败）。
MIGRATION_LOCK_KEY = 7_220_331_001


def migrate(conn: psycopg.Connection) -> list[str]:
    """跑还没跑过的迁移。几个进程同时启动时排队：拿到锁再读哪些跑过了，跑完放锁。"""
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
    conn.commit()
    try:
        return _migrate_locked(conn)
    finally:
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        conn.commit()


def _migrate_locked(conn: psycopg.Connection) -> list[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version text PRIMARY KEY,
          applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()
    applied = {
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    ran: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            continue
        for statement in split_sql(path.read_text(encoding="utf-8")):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
            (path.name,),
        )
        conn.commit()
        ran.append(path.name)
    return ran

"""按文件名顺序执行迁移。语句以行尾分号切开，美元引号里的分号不切开。"""

from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def require_current(conn) -> None:
    """Web 启动和部署只读检查。迁移必须由维护者备份后显式执行。"""
    exists = conn.execute("SELECT to_regclass('schema_migrations') AS name").fetchone()
    if exists["name"] is None:
        raise RuntimeError("数据库尚未初始化；先备份并执行维护，Web 不自动迁移")
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    expected = {path.name for path in MIGRATIONS.glob("*.sql")}
    if applied != expected:
        raise RuntimeError("数据库版本与镜像不一致；先核对备份和迁移计划，禁止自动迁移或降级")


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


def migrate(conn: psycopg.Connection) -> list[str]:
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
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (path.name,),
        )
        conn.commit()
        ran.append(path.name)
    return ran

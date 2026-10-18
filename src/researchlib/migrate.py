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

"""几个进程同时启动、同时跑迁移：排队，只跑一遍，谁都不撞主键退出。"""

from __future__ import annotations

import threading

import psycopg
from psycopg.rows import dict_row

from researchlib import migrate as migrate_module


def test_two_processes_migrating_at_once_both_finish(conn, database_url, monkeypatch, tmp_path):
    # 一条新迁移，跑起来要一会儿：两个连接同时 migrate，不锁的话都看到它没跑过，各跑一遍，后提交的撞主键。
    folder = tmp_path / "migrations"
    folder.mkdir()
    for path in sorted(migrate_module.MIGRATIONS.glob("*.sql")):
        (folder / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (folder / "999_slow_test.sql").write_text(
        "CREATE TABLE IF NOT EXISTS migrate_lock_probe (id int);\nSELECT pg_sleep(0.5);\n", encoding="utf-8"
    )
    monkeypatch.setattr(migrate_module, "MIGRATIONS", folder)
    errors: list[BaseException] = []
    ran: list[list[str]] = []

    def run() -> None:
        try:
            with psycopg.connect(database_url, row_factory=dict_row) as other:
                ran.append(migrate_module.migrate(other))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert sorted(ran, key=len) == [[], ["999_slow_test.sql"]]
    finally:
        conn.execute("DROP TABLE IF EXISTS migrate_lock_probe")
        conn.execute("DELETE FROM schema_migrations WHERE version = '999_slow_test.sql'")
        conn.commit()

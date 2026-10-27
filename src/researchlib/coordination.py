"""部署与数据库任务的会话锁。连接关闭就释放，不杀任务，不用 PID 猜任务状态。"""

from __future__ import annotations

import sys
from contextlib import contextmanager

# 固定的应用专用 advisory lock；Docker Desktop 内外都经同一数据库协调。
LOCK_KEY = 724836290119
PROTOCOL = 1


class DeploymentBusy(RuntimeError):
    pass


def enter_task(conn) -> None:
    if not conn.execute("SELECT pg_try_advisory_lock_shared(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]:
        conn.rollback()
        raise DeploymentBusy("部署正在切换应用，请稍后重试任务")


def leave_task(conn) -> None:
    # 出错的事务也要能释放会话锁。
    if conn.info.transaction_status.name == "INERROR":
        conn.rollback()
    conn.execute("SELECT pg_advisory_unlock_shared(%s)", (LOCK_KEY,))


@contextmanager
def task(conn):
    enter_task(conn)
    try:
        yield
    finally:
        leave_task(conn)


def main() -> int:
    """独立的一次性容器持有部署锁；stdin 关闭或宿主机发送 release 后退出。"""
    from researchlib.config import Settings
    from researchlib.db import connect
    from researchlib.migrate import require_current

    conn = connect(Settings.from_env())
    try:
        conn.autocommit = True
        if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]:
            print("busy", flush=True)
            return 75
        require_current(conn)
        print("ready", flush=True)
        # 只读预检后持锁，独立容器不随 compose up app 被替换。
        sys.stdin.readline()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

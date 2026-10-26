"""显式数据库初始化与维护；Web 启动和读取请求不调用这里。"""

from __future__ import annotations

import argparse

from researchlib.ask import mark_interrupted
from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.duplicates import dedupe_if_needed
from researchlib.mentions import rebuild_if_stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upkeep", action="store_true", help="初始化后，显式检查重复指纹并重建已变化的公司提及")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        mark_interrupted(conn)
        conn.commit()
    finally:
        conn.close()
    if args.upkeep:
        dedupe_if_needed(settings)
        rebuild_if_stale(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""批量 / 单篇站内翻译。需先 research-xai-login login。"""

from __future__ import annotations

import argparse
import sys
import time

from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.translate.service import list_pending_english, translate_article
from researchlib.translate.xai_oauth import XaiLoginRequiredError, xai_oauth_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用站内 Grok OAuth 翻译英文文章")
    parser.add_argument("--article-id", type=int, action="append", default=[], help="指定文章 id，可重复")
    parser.add_argument("--source", default="", help="只处理该来源 id")
    parser.add_argument("--limit", type=int, default=5, help="待译队列最多取几篇")
    parser.add_argument("--loop", action="store_true", help="worker 模式：空闲时休眠后继续扫")
    parser.add_argument("--sleep", type=int, default=60, help="--loop 时空队列休眠秒数")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    status = xai_oauth_status(settings.data_dir)
    if not status.logged_in:
        print("尚未登录 Grok OAuth。先运行：research-xai-login login", file=sys.stderr)
        print(f"token 路径：{status.token_path}", file=sys.stderr)
        return 2

    conn = prepare(settings)
    try:
        while True:
            ids = list(args.article_id) if args.article_id else list_pending_english(
                conn, limit=args.limit, source_id=args.source or None
            )
            if not ids:
                if args.loop:
                    print(f"队列空，{args.sleep}s 后重试…")
                    time.sleep(max(5, args.sleep))
                    continue
                print("没有待译英文文章。")
                return 0
            for article_id in ids:
                try:
                    result = translate_article(conn, article_id, data_dir=settings.data_dir)
                except XaiLoginRequiredError as exc:
                    print(f"需要重新登录：{exc}", file=sys.stderr)
                    return 2
                except Exception as exc:
                    conn.rollback()
                    print(f"#{article_id} failed: {exc}", file=sys.stderr)
                    continue
                print(
                    f"#{result.article_id} {result.status}"
                    + (f" segments={result.segments}" if result.segments else "")
                    + (f" ({result.reason})" if result.reason else "")
                )
            if not args.loop or args.article_id:
                return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

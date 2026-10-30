"""批量 / 单篇站内翻译。默认 DeepSeek；Grok 需要先 research-xai-login login。

在 Mac mini 上：docker compose exec app research-translate --source stratechery --limit 10。
跑的时候自动上线会等它跑完再重建（scripts/deploy.sh）。
"""

from __future__ import annotations

import argparse
import sys
import time

from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.llm_provider import PROVIDER_GROK, deepseek_api_key, llm_provider
from researchlib.translate.service import list_coarse_translations, list_pending_english, translate_article
from researchlib.translate.xai_oauth import XaiLoginRequiredError, xai_oauth_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用站内 LLM（默认 DeepSeek，可切 Grok OAuth）翻译英文文章")
    parser.add_argument("--article-id", type=int, action="append", default=[], help="指定文章 id，可重复")
    parser.add_argument("--source", default="", help="只处理该来源 id")
    parser.add_argument("--limit", type=int, default=5, help="待译队列最多取几篇")
    parser.add_argument("--loop", action="store_true", help="worker 模式：空闲时休眠后继续扫")
    parser.add_argument("--sleep", type=int, default=60, help="--loop 时空队列休眠秒数")
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="重译已经完成、但分段仍是旧粗粒度的篇（大块引用或段数远少于原文）",
    )
    parser.add_argument("--dry-run", action="store_true", help="和 --recheck 一起用：只打印篇目，不调模型")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.dry_run and not args.recheck:
        print("--dry-run 要和 --recheck 一起用", file=sys.stderr)
        return 2
    if args.dry_run:
        conn = prepare(settings)
        try:
            rows = list_coarse_translations(
                conn, limit=max(args.limit, 1), source_id=args.source or None
            )
        finally:
            conn.close()
        for row in rows:
            print(f"#{row['id']} {row['source']} {row['reason']}")
        print(f"共 {len(rows)} 篇")
        return 0
    if llm_provider() == PROVIDER_GROK:
        status = xai_oauth_status(settings.data_dir)
        if not status.logged_in:
            print("尚未登录 Grok OAuth。先运行：research-xai-login login", file=sys.stderr)
            print(f"token 路径：{status.token_path}", file=sys.stderr)
            return 2
    elif not deepseek_api_key():
        print(
            "没有配置 DEEPSEEK_API_KEY。把它写进 .env（或 compose），或设 RESEARCH_LLM_PROVIDER=grok。",
            file=sys.stderr,
        )
        return 2

    conn = prepare(settings)
    # 这一轮失败过的、没有可译正文的 id 先跳过，避免 --limit 很小时反复磕同一篇把队列钉死。
    # --loop 时队列只剩它们就先睡，睡醒清掉跳过再给一次机会：不睡就会一直重试同一篇，每次都花模型的钱。
    skipped: set[int] = set()
    try:
        while True:
            if args.article_id:
                ids = list(args.article_id)
            elif args.recheck:
                rows = list_coarse_translations(
                    conn, limit=max(args.limit, 1) + len(skipped), source_id=args.source or None
                )
                ids = [int(row["id"]) for row in rows]
            else:
                ids = list_pending_english(
                    conn, limit=max(args.limit, 1) + len(skipped), source_id=args.source or None
                )
            if not args.article_id:
                ids = [article_id for article_id in ids if article_id not in skipped][: args.limit]
            if not ids:
                if args.loop and not args.article_id:
                    print(f"队列空，{args.sleep}s 后重试…")
                    time.sleep(max(5, args.sleep))
                    skipped.clear()
                    continue
                print("没有需要重译的文章。" if args.recheck else "没有待译英文文章。")
                return 0
            for article_id in ids:
                try:
                    result = translate_article(conn, article_id, data_dir=settings.data_dir)
                except XaiLoginRequiredError as exc:
                    print(f"需要重新登录：{exc}", file=sys.stderr)
                    return 2
                except Exception as exc:
                    conn.rollback()
                    skipped.add(article_id)
                    print(f"#{article_id} failed: {exc}", file=sys.stderr)
                    continue
                if result.status in ("skipped", "partial"):
                    # 没有可译正文、或者有段没译出来：还在待译队列里，这一轮不再碰。
                    skipped.add(article_id)
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

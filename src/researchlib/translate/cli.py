"""批量 / 单篇站内翻译。默认 DeepSeek；Grok 需要先 research-xai-login login。

在 Mac mini 上：docker compose exec app research-translate --source stratechery --limit 10。
跑的时候自动上线先不重建（scripts/deploy.sh）。它在等的提交 CI 过了，就写 status/.deploy-waiting：
按队列译（包括 --loop）时每译完一篇看一眼，在就停下，让新版本上线，上线后再跑。点名的 --article-id 译完才停。

点名的几篇被断线、超时、限流、5xx 打断（或者译的时候原文被别的任务改了）没译完的，
最后等 2 分钟再补一轮，还不行等 10 分钟再补一轮；已译的段都存着，再补只问没译出来的段。
等的时候自动上线来请它停，就不补了，印出上线后要再跑的命令。
重发同一请求、拆批、先放英文这些过程带钟点（UTC）印在 stderr。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from researchlib.config import Settings
from researchlib.db import connect, prepare
from researchlib.health import deploy_waiting
from researchlib.llm_provider import PROVIDER_GROK, deepseek_api_key, llm_provider
from researchlib.translate.llm import transient_failure
from researchlib.translate.provenance import AlignmentError
from researchlib.translate.service import (
    list_coarse_translations,
    list_pending_english,
    record_translation_failure,
    translate_article,
)
from researchlib.translate.xai_oauth import XaiLoginRequiredError, xai_oauth_status

# 点名补译时，被打断没译完的篇隔这么久再补一轮（秒），最多补这么多轮。
# 按队列译的不用：没译完的记了冷却，过了冷却自己会回到队列。
NAMED_RETRY_PAUSES = (120, 600)
# 等的时候每隔这么久看一眼自动上线在不在等，在等就不补了，免得把上线挡十几分钟。
PAUSE_STEP = 15


class _ProgressLines(logging.Handler):
    """译制日志一行一行印到当下的 stderr。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            print(self.format(record), file=sys.stderr, flush=True)
        except Exception:
            self.handleError(record)


def _show_progress():
    """把译制过程（重发同一请求、拆批、先放英文、请求失败）带钟点印出来。返回撤掉它的函数。

    以前这些只写 INFO 日志，命令行没配日志，一行都看不到：SSL 断了重试了几次、每次连了多久都不知道。
    """
    log = logging.getLogger("researchlib.translate")
    handler = _ProgressLines()
    formatter = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    level = log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def undo() -> None:
        log.removeHandler(handler)
        log.setLevel(level)

    return undo


def _retry_later(error: BaseException) -> bool:
    """隔几分钟再跑同一篇多半能补上：服务那头断线、超时、限流、5xx，或者译到一半原文被别的任务改了（夜间补钟点）。"""
    if transient_failure(error):
        return True
    return isinstance(error, AlignmentError) and error.code.endswith("source-changed-during-request")


def _command(ids: list[int], fresh: bool) -> str:
    return "research-translate " + " ".join(f"--article-id {article_id}" for article_id in ids) + (
        " --fresh" if fresh else ""
    )


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
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="整篇重译，不复用已有译文。默认原文没变的段直接用已有译文，只补没译出来和原文变了的段",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.dry_run and not args.recheck:
        print("--dry-run 要和 --recheck 一起用", file=sys.stderr)
        return 2
    if args.dry_run:
        conn = connect(settings)
        try:
            conn.execute("SET TRANSACTION READ ONLY")
            rows = list_coarse_translations(
                conn, limit=max(args.limit, 1), source_id=args.source or None
            )
        finally:
            conn.rollback()
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

    def make_way() -> bool:
        # 自动上线在等：按队列译的不再开新的一篇。旧容器里的进程译的是旧规则，--loop 又不会自己停，
        # 不让路的话之后合并的改动（包括译制自己的修复）一直上不了线。点名的几篇译完再说。
        if args.article_id:
            return False
        waiting = deploy_waiting(settings.data_dir)
        if waiting:
            print(f"自动上线在等 {waiting[:7]}：先停在这里，/health 报出新提交后再跑 research-translate")
        return bool(waiting)

    if make_way():
        return 0
    conn = prepare(settings)
    # 本进程里失败或没译完的 id 先跳过。库里的 translation_attempts 另有冷却，
    # 进程重启也不会把刚拒绝的文章立刻拿回来。
    skipped: set[int] = set()
    # 出错、到最后也没补上的篇。点名补译时后面那一轮补上了就拿掉。
    failed: set[int] = set()

    def run(article_id: int) -> bool:
        """译一篇、印一行结果。被打断、隔几分钟再跑多半能补上的返回 True。"""
        try:
            result = translate_article(conn, article_id, data_dir=settings.data_dir, reuse=not args.fresh)
        except XaiLoginRequiredError:
            raise
        except Exception as exc:
            conn.rollback()
            failed.add(article_id)
            skipped.add(article_id)
            try:
                record_translation_failure(conn, article_id, exc)
            except Exception:
                conn.rollback()
                print(f"#{article_id} 失败回执未能保存", file=sys.stderr)
            print(f"#{article_id} failed: {exc}", file=sys.stderr, flush=True)
            return _retry_later(exc)
        failed.discard(article_id)
        if result.status in ("skipped", "partial"):
            # 没有可译正文、或者有段没译出来：还在待译队列里，这一轮不再碰。
            # 没译完的已经存下，过了冷却再进队列，下次只补没译出来的段。
            skipped.add(article_id)
        print(
            f"#{result.article_id} {result.status}"
            + (f" segments={result.segments}" if result.segments else "")
            # 原文没变、直接用了已有译文的段数。
            + (f" reused={result.reused}" if result.reused else "")
            # 没译出来、先放英文的段数。序号在回执的 untranslated 里。
            + (f" untranslated={result.untranslated}" if result.untranslated else "")
            # 数字没能自动核对上、照样写入的段数。序号在回执的 number_review 里。
            + (f" number_review={result.review}" if result.review else "")
            + (f" ({result.reason})" if result.reason else "")
            # 请求失败时那一次的报错（断线、限流、欠费），点名再跑这篇会接着译。
            + (f" {result.error}" if result.error else ""),
            flush=True,
        )
        return result.status == "partial" and result.retry_later

    def pause(seconds: int) -> str:
        """睡 seconds 秒。中间自动上线来请它停，就提前醒，返回它在等的提交。"""
        left = seconds
        while left > 0:
            waiting = deploy_waiting(settings.data_dir)
            if waiting:
                return waiting
            step = min(PAUSE_STEP, left)
            time.sleep(step)
            left -= step
        return deploy_waiting(settings.data_dir)

    def finish_named(again: list[int]) -> int:
        """点名补译里被打断的篇，隔一会儿再补，最多补 len(NAMED_RETRY_PAUSES) 轮。"""
        for seconds in NAMED_RETRY_PAUSES:
            if not again:
                break
            listed = " ".join(f"#{article_id}" for article_id in again)
            print(f"{listed} 被打断没译完，{seconds // 60} 分钟后再补一轮", flush=True)
            waiting = pause(seconds)
            if waiting:
                print(f"自动上线在等 {waiting[:7]}：不等着补了，/health 报出新提交后再跑 {_command(again, args.fresh)}")
                return 1
            again = [article_id for article_id in again if run(article_id)]
        if again:
            listed = " ".join(f"#{article_id}" for article_id in again)
            print(f"补了 {len(NAMED_RETRY_PAUSES)} 轮，{listed} 还没译完：过一会儿再跑 {_command(again, args.fresh)}")
            return 1
        return 1 if failed else 0

    undo_progress = _show_progress()
    try:
        while True:
            if make_way():
                return 1 if failed else 0
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
                    # 库里的冷却已经挡住刚失败的篇；这里只是避免空转时立刻再查。
                    continue
                print("没有需要重译的文章。" if args.recheck else "没有待译英文文章。")
                return 1 if failed else 0
            again: list[int] = []
            for number, article_id in enumerate(ids):
                if number and make_way():
                    return 1 if failed else 0
                if run(article_id):
                    again.append(article_id)
            if args.article_id:
                return finish_named(again)
            if not args.loop:
                return 1 if failed else 0
    except XaiLoginRequiredError as exc:
        print(f"需要重新登录：{exc}", file=sys.stderr)
        return 2
    finally:
        undo_progress()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

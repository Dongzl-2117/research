"""批量 / 单篇站内翻译。默认 DeepSeek；Grok 需要先 research-xai-login login。

在 Mac mini 上：docker compose exec app research-translate --source stratechery --limit 10。
``--workers N`` 几篇同时译：每篇里的各批照旧一批接一批（每批要带上前面译好的中文），篇和篇之间互不等。
``--from-inventory 类别`` 译盘点清单（research-translate-inventory）里那一类的篇，比如分开展示的旧稿 unmapped、
拒满两次停住的 stopped；已经有这一版回执、译完的跳过，中途停下再跑同一条命令接着译剩下的。
每篇开译前占一把 Postgres advisory lock，同一进程的几个 worker、同时开着的几个 research-translate
都不会同时译同一篇；点名的那篇正被别处译着，就等它译完再接着补。
每篇译完印出这一篇的请求次数、输入（其中缓存命中）、输出（其中思考）token 和用时；调过模型的话，
停下时再印一行这一轮的合计。

跑的时候自动上线先不重建（scripts/deploy.sh）。它在等的提交 CI 过了，就写 status/.deploy-waiting：
按队列译（包括 --loop）时每个 worker 译完手上那篇看一眼，在就停下，让新版本上线，上线后再跑。
点名的 --article-id 译完才停。

点名的几篇被断线、超时、限流、5xx 打断（或者译的时候原文被别的任务改了）没译完的，
最后等 2 分钟再补一轮，还不行等 10 分钟再补一轮；已译的段都存着，再补只问没译出来的段。
等的时候自动上线来请它停，就不补了，印出上线后要再跑的命令。
重发同一请求、拆批、先放英文这些过程带钟点（UTC）和文章号印在 stderr。
"""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar

from researchlib.config import Settings
from researchlib.db import connect, ensure_search_session, prepare
from researchlib.health import deploy_waiting
from researchlib.llm_provider import PROVIDER_GROK, deepseek_api_key, llm_provider
from researchlib.translate import inventory
from researchlib.translate.llm import Usage, metered, transient_failure
from researchlib.translate.provenance import AlignmentError
from researchlib.translate.service import (
    TranslateResult,
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
# 最多几篇同时译。每个 worker 占一条数据库连接，同一时刻最多发一个模型请求。
MAX_WORKERS = 16
# 每篇的锁是 (hashtext(LOCK_NAME), 文章 id) 这把会话级 advisory lock：译完放掉，进程没了自动放掉。
LOCK_NAME = "research-translate"

_PRINT = threading.Lock()
# 这个线程正在译的文章。几篇同时译时，译制日志的每一行前面带上它，分得清是哪一篇的。
_translating: ContextVar[int | None] = ContextVar("translating", default=None)


def _say(text: str, *, err: bool = False) -> None:
    """印一整行。几个 worker 同时印，也不会把两行搅在一起。"""
    with _PRINT:
        print(text, file=sys.stderr if err else sys.stdout, flush=True)


class _ProgressLines(logging.Handler):
    """译制日志一行一行印到当下的 stderr。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _say(self.format(record), err=True)
        except Exception:
            self.handleError(record)


class _ArticleTag(logging.Filter):
    """日志行前面加上这个线程正在译的文章号；本来就以它开头的行不再重复加。"""

    def filter(self, record: logging.LogRecord) -> bool:
        current = _translating.get()
        tag = f"#{current} " if current is not None else ""
        try:
            already = bool(tag) and record.getMessage().startswith(tag)
        except Exception:  # noqa: BLE001 — 参数对不上的日志照样印，交给 Handler 报
            already = False
        record.translating_tag = tag if tag and not already else ""
        return True


def _show_progress():
    """把译制过程（重发同一请求、拆批、先放英文、请求失败）带钟点和文章号印出来。返回撤掉它的函数。

    以前这些只写 INFO 日志，命令行没配日志，一行都看不到：SSL 断了重试了几次、每次连了多久都不知道。
    """
    log = logging.getLogger("researchlib.translate")
    handler = _ProgressLines()
    handler.addFilter(_ArticleTag())
    formatter = logging.Formatter("%(asctime)s %(translating_tag)s%(message)s", "%H:%M:%S")
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


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _spent(usage: Usage, seconds: float) -> str:
    """一篇的用量和用时，接在结果那一行后面。没调模型（全是复用的、没正文）就不印。"""
    if not usage.requests:
        return ""
    return f" · {usage.describe()}，用时 {_duration(seconds)}"


def _try_lock(conn, article_id: int) -> bool:
    """占住这一篇。别的 worker、别的 research-translate 正在译它，就返回 False。"""
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s), %s) AS locked", (LOCK_NAME, article_id)
    ).fetchone()
    conn.commit()
    return bool(row and row["locked"])


def _wait_lock(conn, article_id: int) -> None:
    """等别处把这一篇译完、放掉锁，再占住它。"""
    conn.execute("SELECT pg_advisory_lock(hashtext(%s), %s)", (LOCK_NAME, article_id))
    conn.commit()


def _unlock(conn, article_id: int) -> None:
    """放掉这一篇。连接已经断了的话，锁随会话一起没了，不用管。"""
    for _attempt in range(2):
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s), %s)", (LOCK_NAME, article_id))
            conn.commit()
            return
        except Exception:  # noqa: BLE001 — 事务坏了先回滚再放一次
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                return


def _open_worker(settings: Settings):
    """再开一条连接给第二个起的 worker，检索切词设置和 prepare 那条一样（译文切块进检索要用）。"""
    conn = connect(settings)
    try:
        ensure_search_session(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用站内 LLM（默认 DeepSeek，可切 Grok OAuth）翻译英文文章")
    parser.add_argument("--article-id", type=int, action="append", default=[], help="指定文章 id，可重复")
    parser.add_argument("--source", default="", help="只处理该来源 id")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="待译队列最多取几篇，默认 5（--loop 时是每次查队列取几篇；--from-inventory 默认整类）",
    )
    parser.add_argument("--loop", action="store_true", help="worker 模式：空闲时休眠后继续扫")
    parser.add_argument("--sleep", type=int, default=60, help="--loop 时空队列休眠秒数")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=f"几篇同时译（1 到 {MAX_WORKERS}）。每篇里的各批照旧一批接一批",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="重译已经完成、但分段仍是旧粗粒度的篇（大块引用或段数远少于原文）",
    )
    parser.add_argument("--dry-run", action="store_true", help="和 --recheck 一起用：只打印篇目，不调模型")
    parser.add_argument(
        "--from-inventory",
        default="",
        metavar="类别",
        help="译盘点清单里这一类的篇（unmapped、stopped、legacy、coarse……）。已经有这一版回执、译完的跳过",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="整篇重译，不复用已有译文。默认原文没变的段直接用已有译文，只补没译出来和原文变了的段",
    )
    args = parser.parse_args(argv)
    # 让路以后原样再跑这一条。
    again_command = " ".join(["research-translate", *map(shlex.quote, sys.argv[1:] if argv is None else argv)])

    settings = Settings.from_env()
    if not 1 <= args.workers <= MAX_WORKERS:
        print(f"--workers 要在 1 到 {MAX_WORKERS} 之间", file=sys.stderr)
        return 2
    if args.dry_run and not args.recheck:
        print("--dry-run 要和 --recheck 一起用", file=sys.stderr)
        return 2
    if args.from_inventory and (args.article_id or args.recheck or args.loop or args.source):
        print("--from-inventory 不和 --article-id、--recheck、--loop、--source 一起用", file=sys.stderr)
        return 2
    if args.dry_run:
        conn = connect(settings)
        try:
            conn.execute("SET TRANSACTION READ ONLY")
            rows = list_coarse_translations(
                conn, limit=max(args.limit or 5, 1), source_id=args.source or None
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

    named = list(dict.fromkeys(args.article_id))
    # 盘点清单里的这一类，按清单的先后（新的在前）。没清单、没这一类就别连库了。
    listed: list[int] = []
    if args.from_inventory:
        try:
            listed, generated = inventory.listed(settings.data_dir, args.from_inventory)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 2
        _say(
            f"盘点清单（{generated or '时间不详'}）里 {args.from_inventory} 共 {len(listed)} 篇，"
            "已经有这一版回执、译完的跳过"
        )
    limit = max(args.limit if args.limit is not None else (len(listed) if args.from_inventory else 5), 1)
    # 几个 worker 共用的东西都在 guard 底下改。
    guard = threading.Lock()
    stop = threading.Event()
    made_way = threading.Event()
    # 本进程这一轮已经拿过的篇（包括正在译的），按队列译时不再拿。库里的 translation_attempts 另有冷却，
    # 进程重启也不会把刚拒绝的文章立刻拿回来。
    tried: set[int] = set()
    # 出错、到最后也没补上的篇。点名补译时后面那一轮补上了就拿掉。
    failed: set[int] = set()
    # 按队列译时查出来、还没分出去的篇。
    queued: list[int] = []
    flags = {"queried": False, "idle": False}
    tally: Counter[str] = Counter()
    total = Usage()
    started = time.monotonic()

    def make_way() -> bool:
        # 自动上线在等：按队列译的不再开新的一篇。旧容器里的进程译的是旧规则，--loop 又不会自己停，
        # 不让路的话之后合并的改动（包括译制自己的修复）一直上不了线。点名的几篇译完再说。
        if named:
            return False
        waiting = deploy_waiting(settings.data_dir)
        if waiting and not made_way.is_set():
            with guard:
                first = not made_way.is_set()
                made_way.set()
            if first:
                _say(
                    f"自动上线在等 {waiting[:7]}：不再开新的一篇，手上的译完就退；"
                    f"/health 报出新提交后再跑 {again_command}"
                )
        return bool(waiting)

    if make_way():
        return 0
    # 点名的、清单里的篇比 worker 少，就只开够用的连接。
    workers = args.workers
    if named:
        workers = min(workers, len(named))
    elif args.from_inventory:
        workers = max(1, min(workers, len(listed)))
    conns = [prepare(settings)]
    try:
        for _number in range(workers - 1):
            conns.append(_open_worker(settings))
    except Exception:
        for conn in conns:
            conn.close()
        raise

    def run(conn, article_id: int) -> bool:
        """译一篇、印一行结果和这篇的用量。被打断、隔几分钟再跑多半能补上的返回 True。"""
        token = _translating.set(article_id)
        begun = time.monotonic()
        result: TranslateResult | None = None
        error: Exception | None = None
        try:
            with metered() as usage:
                try:
                    result = translate_article(conn, article_id, data_dir=settings.data_dir, reuse=not args.fresh)
                except XaiLoginRequiredError:
                    conn.rollback()
                    raise
                except Exception as exc:  # noqa: BLE001 — 一篇出错不停，记下接着译别的
                    conn.rollback()
                    error = exc
                    try:
                        record_translation_failure(conn, article_id, exc)
                    except Exception:  # noqa: BLE001
                        conn.rollback()
                        _say(f"#{article_id} 失败回执未能保存", err=True)
        finally:
            _translating.reset(token)
        spent = _spent(usage, time.monotonic() - begun)
        with guard:
            total.merge(usage)
            tally[result.status if result is not None else "failed"] += 1
            if result is None:
                failed.add(article_id)
            else:
                failed.discard(article_id)
        if result is None:
            _say(f"#{article_id} failed: {error}{spent}", err=True)
            return _retry_later(error)
        _say(
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
            + (f" {result.error}" if result.error else "")
            + spent
        )
        return result.status == "partial" and result.retry_later

    def drain(take: Callable[[object], int | None], work: Callable[[object, int], None]) -> None:
        """每条连接一个 worker：take 拿下一篇（已经占住锁），work 译它，译完放锁；take 交回 None 就停。

        worker 里抛出来的异常（要重新登录、测试里的假 sleep）先让别的 worker 做完手上那篇停下，再在这里原样抛出。
        """
        errors: list[BaseException] = []

        def loop(conn) -> None:
            try:
                while not stop.is_set():
                    article_id = take(conn)
                    if article_id is None:
                        return
                    try:
                        work(conn, article_id)
                    finally:
                        _unlock(conn, article_id)
            except BaseException as exc:  # noqa: BLE001 — 交给主线程原样抛
                with guard:
                    errors.append(exc)
                stop.set()

        threads = [
            threading.Thread(target=loop, args=(conn,), name=f"research-translate-{number}", daemon=True)
            for number, conn in enumerate(conns, 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]

    def claim(conn) -> int | None:
        """队列里下一篇本进程这一轮没拿过、别处也没在译的，占住它。手上的分完了再查一次队列（--loop 时每次都查）。"""
        refilled = False
        while True:
            while queued:
                article_id = queued.pop(0)
                if article_id not in tried and _try_lock(conn, article_id):
                    tried.add(article_id)
                    return article_id
            if refilled or (flags["queried"] and not args.loop):
                return None
            if args.recheck:
                rows = list_coarse_translations(conn, limit=limit + len(tried), source_id=args.source or None)
                ids = [int(row["id"]) for row in rows]
            elif args.from_inventory:
                ids = inventory.unfinished(conn, listed)
            else:
                ids = list_pending_english(conn, limit=limit + len(tried), source_id=args.source or None)
            # 只读的查询，别让事务在空队列睡觉时一直开着。
            conn.commit()
            flags["queried"] = refilled = True
            queued.extend([article_id for article_id in ids if article_id not in tried][:limit])

    def take_queued(conn) -> int | None:
        while not stop.is_set():
            if make_way():
                stop.set()
                return None
            with guard:
                if not args.loop and len(tried) >= limit:
                    return None
                article_id = claim(conn)
                if article_id is not None:
                    flags["idle"] = False
                    return article_id
                if not args.loop:
                    return None
                announce = not flags["idle"]
                flags["idle"] = True
            if announce:
                _say(f"队列空，{args.sleep}s 后重试…")
            time.sleep(max(5, args.sleep))
            # 库里的冷却已经挡住刚失败的篇；这里只是避免空转时立刻再查。
        return None

    def run_named(ids: list[int]) -> list[int]:
        """点名的这几篇各译一遍，交回被打断、隔几分钟再补多半能补上的（按点名的先后）。"""
        pending = list(ids)
        again: set[int] = set()

        def take(conn) -> int | None:
            with guard:
                if not pending:
                    return None
                article_id = pending.pop(0)
            if not _try_lock(conn, article_id):
                _say(f"#{article_id} 别处正在译这一篇，等它译完再接着补")
                _wait_lock(conn, article_id)
            return article_id

        def work(conn, article_id: int) -> None:
            if run(conn, article_id):
                with guard:
                    again.add(article_id)

        drain(take, work)
        return [article_id for article_id in ids if article_id in again]

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
            _say(f"{listed} 被打断没译完，{seconds // 60} 分钟后再补一轮")
            waiting = pause(seconds)
            if waiting:
                _say(f"自动上线在等 {waiting[:7]}：不等着补了，/health 报出新提交后再跑 {_command(again, args.fresh)}")
                return 1
            again = run_named(again)
        if again:
            listed = " ".join(f"#{article_id}" for article_id in again)
            _say(f"补了 {len(NAMED_RETRY_PAUSES)} 轮，{listed} 还没译完：过一会儿再跑 {_command(again, args.fresh)}")
            return 1
        return 1 if failed else 0

    def summary() -> None:
        """这一轮的合计。没调过模型就不印（没花钱，逐篇那几行已经够了）。"""
        if not total.requests:
            return
        names = {"translated": "译完", "partial": "没译完", "failed": "出错", "skipped": "跳过"}
        counts = "，".join(f"{names.get(status, status)} {n} 篇" for status, n in tally.most_common())
        _say(f"这一轮 {_duration(time.monotonic() - started)}：{counts}；{total.describe()}")

    undo_progress = _show_progress()
    interrupted = False
    try:
        if named:
            return finish_named(run_named(named))
        drain(take_queued, run)
        if not tried and not made_way.is_set():
            if args.from_inventory:
                _say(f"{args.from_inventory} 这一类都已经译完、有这一版回执了。")
            else:
                _say("没有需要重译的文章。" if args.recheck else "没有待译英文文章。")
        return 1 if failed else 0
    except XaiLoginRequiredError as exc:
        print(f"需要重新登录：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # 手上的几篇还在别的线程里等模型回答，进程一退就断了：这几篇这次不存，下次重新译。
        interrupted = True
        stop.set()
        _say("Ctrl-C：停下了。正在译的几篇这次不存，下次重新译", err=True)
        return 130
    finally:
        summary()
        undo_progress()
        if not interrupted:
            for conn in conns:
                conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

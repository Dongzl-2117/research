"""花模型钱的批量命令共用的跑法：几篇同时处理、每篇占锁、记用量、避开高峰价、余额不足整批停。

research-views、research-digest 用。命令自己查出要处理的文章，交给 ``run`` 一个 ``handle(conn, job, usage)``：
处理一篇，交回印在这一篇结果行里的话；交回 None 是这一篇不用处理（别的进程刚处理完、文章没了），算跳过。

- 每个 worker 一条自己的数据库连接，同一时刻最多发一个模型请求。
- 每篇先占一把 Postgres advisory lock：(hashtext(锁名), 文章 id)，会话级，处理完放掉，进程没了自动放掉。
  同一进程的几个 worker、同时开着的几个命令、网页后台那条队列都不会处理同一篇。
- 每篇印一行用量和用时，最后的合计由命令自己印（``tail``），拿它算实际花了多少钱。
- 没凭证、接口拒绝鉴权、余额不足（402）整批停，不把剩下的一篇篇记成失败。单篇别的错只记这一篇，隔几天再试。
- 限流、5xx、断网、超时（``transient``）先不记：连着 ``PAUSE_AFTER`` 篇都这样就整批暂停，这几篇不记失败，
  命令退出码 3，外面的循环过几分钟再跑同一条。中间有一篇成功了，说明接口是通的，前面那几篇才照常记失败。
- 一次批量的进度都在 ``run`` 的局部变量里，不放模块全局变量。
- ``verb`` 是印给人看的动词（观点是「整理」、摘要是「写」），只影响提示的字。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from researchlib.config import Settings
from researchlib.deepseek import deepseek_peak_until
from researchlib.failures import note_failure
from researchlib.llm import LLMError, LLMUnavailable
from researchlib.llm_provider import PROVIDER_DEEPSEEK, llm_provider
from researchlib.textutil import SHANGHAI

# 最多几篇同时来。
MAX_WORKERS = 32
# DeepSeek 高峰价时段里，等着的 worker 隔这么久（秒）看一眼过没过高峰。
PEAK_CHECK_S = 60.0
# 连着这么多篇都是限流、5xx、断网、超时，就整批暂停（llm.chat 每篇已经隔 2、5、15、30 秒重试过）。
PAUSE_AFTER = 3
# 整批暂停时命令的退出码：外面的循环过几分钟再跑同一条，整理过的不会重来。
EXIT_PAUSED = 3
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)


def claim(conn, lock_name: str, item_id: int) -> bool:
    """占这一篇的锁。占不到是别的 worker、别的进程正在处理它。"""
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s), %s) AS locked", (lock_name, int(item_id))
    ).fetchone()
    conn.commit()
    return bool(row["locked"])


def release(conn, lock_name: str, item_id: int) -> None:
    # 处理到一半出错时事务是坏的，先回滚才放得掉锁。成功的那次已经提交过，回滚不丢东西。
    conn.rollback()
    conn.execute("SELECT pg_advisory_unlock(hashtext(%s), %s)", (lock_name, int(item_id)))
    conn.commit()


def fatal(exc: Exception) -> bool:
    """整批停：没凭证，接口拒绝鉴权，或余额不足（402）。单篇 5xx、读不出 JSON 不算。

    余额用完以后每篇都马上失败，接着跑只会把剩下的一篇篇记成失败、各自冷却好几天。
    """
    if isinstance(exc, LLMUnavailable):
        return True
    text = str(exc)
    return any(f"接口返回 {code}" in text for code in (401, 402, 403))


def transient(exc: Exception) -> bool:
    """接口一时不通：限流、5xx、连不上、读超时、回答读到一半断了。和这一篇的内容无关，过一会儿再试多半就好。

    ``llm.chat`` 的报错：「… 接口返回 429：…」「连不上 … 接口：…」（断线、超时、流式回答中途断开都是这一句）。
    """
    if isinstance(exc, LLMUnavailable):
        return False
    text = str(exc)
    return text.startswith("连不上 ") or any(f"接口返回 {code}" in text for code in _TRANSIENT_STATUS)


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def spent(usage: dict, seconds: float) -> str:
    """用量和用时，接在结果那一行后面。接口没报用量就只印用时。"""
    took = f"用时 {_duration(seconds)}"
    if not usage.get("prompt") and not usage.get("completion"):
        return took
    thinking = f"（其中思考 {usage['reasoning']:,}）" if usage.get("reasoning") else ""
    return f"进 {usage.get('prompt', 0):,} 出 {usage.get('completion', 0):,}{thinking} token，{took}"


def tail(report: dict, verb: str = "处理") -> str:
    """合计那一行的后半句：跳过了几篇、一共用了多少 token、多久。"""
    skipped = f"，跳过 {report['skipped']} 篇（别处正在{verb}或已经{verb}过）" if report["skipped"] else ""
    return f"{skipped} · 合计{spent(report['usage'], report['seconds'])}"


def exit_code(report: dict, verb: str = "处理") -> int:
    """命令的退出码，顺带印出为什么停：0 跑完了（个别文章失败也算），2 整批停下（没凭证、鉴权被拒、余额不足），
    3 连着几篇接口不通、暂停了（外面的循环过几分钟再跑同一条）。"""
    if report["fatal"]:
        print(f"整批停下：{report['fatal']}", file=sys.stderr)
        return 2
    if report.get("paused"):
        print(
            f"暂停：连着 {PAUSE_AFTER} 篇接口不通（{report['paused']}），这几篇没记失败。"
            f"过几分钟再跑同一条，{verb}过的不会重来",
            file=sys.stderr,
        )
        return EXIT_PAUSED
    return 0


def add_arguments(parser: argparse.ArgumentParser, verb: str = "处理") -> None:
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=f"几篇同时{verb}（1 到 {MAX_WORKERS}，默认 1）。超过 RESEARCH_LLM_CONCURRENCY 的部分不起作用",
    )
    parser.add_argument(
        "--off-peak",
        action="store_true",
        help=(
            "走 DeepSeek 时避开高峰价时段（北京时间工作日 9 到 12 点、14 到 18 点）："
            f"高峰不开新的一篇，过了接着{verb}"
        ),
    )


def check_arguments(args: argparse.Namespace) -> None:
    if not 1 <= args.workers <= MAX_WORKERS:
        raise SystemExit(f"--workers 要在 1 到 {MAX_WORKERS} 之间")


def resolve(settings: Settings, args: argparse.Namespace, verb: str = "处理") -> tuple[int, bool]:
    """按这次的配置定下来：实际几篇同时处理、要不要避开高峰价。"""
    workers = args.workers
    concurrency = max(1, int(getattr(settings, "llm_concurrency", 8) or 8))
    if workers > concurrency:
        print(
            f"同时{verb} {concurrency} 篇：--workers {workers} 超过 RESEARCH_LLM_CONCURRENCY，"
            f"要开满就在命令前加 -e RESEARCH_LLM_CONCURRENCY={workers}",
            file=sys.stderr,
        )
        workers = concurrency
    provider = getattr(settings, "llm_provider", "") or llm_provider()
    return workers, bool(args.off_peak) and provider == PROVIDER_DEEPSEEK


def run(
    settings: Settings,
    jobs: list[dict],
    handle: Callable[[object, dict, dict], str | None],
    *,
    kind: str,
    lock_name: str,
    verb: str = "处理",
    workers: int = 1,
    deadline: float | None = None,
    off_peak: bool = False,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """把 jobs 处理完。jobs：[{"id", "title", …}]，别的键给 handle 用。

    kind：失败记进 llm_failures 的那一类（views、digest）。
    off_peak：DeepSeek 高峰价时段不开新的一篇，等高峰过了接着跑；手上的那篇照常处理完。
    deadline：``time.monotonic()`` 到了这个数就不再开新的一篇。
    交回 {"done", "failed", "skipped", "usage", "fatal", "paused", "timed_out", "seconds"}；
    fatal 不为空是整批停下的原因（命令退出码 2），paused 不为空是连着几篇接口不通、整批暂停（退出码 3）。
    failed 只算记进 llm_failures 的；接口不通、还没轮到记的几篇不算，下次照常排队。
    """
    from researchlib.db import connect, ensure_search_session

    guard = threading.Lock()
    printing = threading.Lock()
    queue = iter(jobs)
    report: dict = {"done": 0, "failed": 0, "skipped": 0, "usage": {}, "fatal": "", "paused": "", "timed_out": False}
    # 接口不通、还没记失败的几篇：(文章 id, 错误)。连着 PAUSE_AFTER 篇就暂停；中间有一篇成功就把它们记成失败。
    held: list[tuple[int, Exception]] = []
    stop = threading.Event()
    peak: dict = {"until": None}
    crashed: list[BaseException] = []
    clock = now or (lambda: datetime.now(UTC))
    started = time.monotonic()

    def say(text: str, *, err: bool = False) -> None:
        with printing:
            print(text, file=sys.stderr if err else sys.stdout, flush=True)

    def bump(name: str) -> None:
        with guard:
            report[name] += 1

    def settle(conn) -> None:
        """接口是通的（这一篇有了结果）：前面接口不通的那几篇是它们自己的事，照常记失败。"""
        with guard:
            if report["paused"]:
                return
            pending, held[:] = list(held), []
        for item_id, exc in pending:
            note_failure(conn, kind, item_id, exc)
            bump("failed")

    def hold(item_id: int, exc: Exception) -> None:
        """接口不通：先不记。连着 PAUSE_AFTER 篇都这样就整批暂停，这几篇一篇都不记。"""
        with guard:
            if report["paused"]:
                return
            held.append((item_id, exc))
            if len(held) < PAUSE_AFTER:
                return
            report["paused"] = str(exc)
            held.clear()
        stop.set()

    def wait_peak() -> bool:
        if not off_peak:
            return False
        moment = clock()
        until = deepseek_peak_until(moment)
        with guard:
            before, peak["until"] = peak["until"], until
        if until is None:
            if before is not None:
                say(f"过了 DeepSeek 高峰价时段，接着{verb}")
            return False
        if until != before:
            say(
                "DeepSeek 高峰价时段（北京时间工作日 9 到 12 点、14 到 18 点，单价翻倍）：不开新的一篇，"
                f"北京时间 {until.astimezone(SHANGHAI):%H:%M} 以后接着{verb}。高峰也要{verb}就去掉 --off-peak"
            )
        sleep(max(1.0, min(PEAK_CHECK_S, (until - moment).total_seconds())))
        return True

    def one(conn, job: dict) -> None:
        item_id = int(job["id"])
        title = " ".join(str(job.get("title") or "").split())
        if not claim(conn, lock_name, item_id):
            bump("skipped")
            return
        try:
            usage: dict = {}
            begun = time.monotonic()
            try:
                line = handle(conn, job, usage)
            except (LLMUnavailable, LLMError) as exc:
                conn.rollback()
                if fatal(exc):
                    with guard:
                        report["fatal"] = report["fatal"] or str(exc)
                    stop.set()
                    return
                say(f"{item_id} {title}: {exc}", err=True)
                if transient(exc):
                    hold(item_id, exc)
                    return
                settle(conn)
                note_failure(conn, kind, item_id, exc)
                bump("failed")
                return
            if line is None:
                bump("skipped")
                return
            settle(conn)
            with guard:
                report["done"] += 1
                for name, value in usage.items():
                    report["usage"][name] = report["usage"].get(name, 0) + value
            say(f"{item_id} {title[:40]}：{line} · {spent(usage, time.monotonic() - begun)}")
        finally:
            release(conn, lock_name, item_id)

    def work() -> None:
        conn = None
        try:
            conn = connect(settings)
            ensure_search_session(conn)
            conn.commit()
            while not stop.is_set():
                if deadline is not None and time.monotonic() > deadline:
                    with guard:
                        report["timed_out"] = True
                    return
                if wait_peak():
                    continue
                with guard:
                    job = next(queue, None)
                if job is None:
                    return
                one(conn, job)
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常带回主线程再抛，别的 worker 一起停
            crashed.append(exc)
            stop.set()
        finally:
            if conn is not None:
                conn.close()

    count = max(1, min(int(workers), len(jobs) or 1))
    if count == 1:
        work()
    else:
        threads = [
            threading.Thread(target=work, name=f"{kind}-{index + 1}", daemon=True) for index in range(count)
        ]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                thread.join()
        except KeyboardInterrupt:
            # 第一次 Ctrl-C：不开新的一篇，手上的处理完再退，已经花掉的推理不白花。再按一次立刻退。
            stop.set()
            say(f"收到停止：手上的几篇{verb}完就退，再按一次 Ctrl-C 立刻退", err=True)
            for thread in threads:
                thread.join()
    if crashed:
        raise crashed[0]
    report["seconds"] = time.monotonic() - started
    return report

"""站内译制 worker：由 HTTP 接口触发，在应用进程里开一个后台线程跑。

和 ``research-translate`` CLI 用同一套 ``list_pending_english`` / ``translate_article``，
不另开进程，也不在应用启动时自动跑。同一时间只允许一个 worker：已经在跑时再触发只回当前状态。

loop 模式和 CLI 一样：队列空了休眠一会儿再扫；本轮失败过的 id 先跳过，等下一轮再试。
stop 只停在下一次检查点，正在译的那篇会译完（长文可能要几分钟）。
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from researchlib.config import Settings
from researchlib.llm import active_model_id, llm_configured
from researchlib.llm_provider import PROVIDER_GROK, llm_provider, provider_label
from researchlib.translate.service import list_pending_english, translate_article
from researchlib.translate.xai_oauth import XaiLoginRequiredError

logger = logging.getLogger("researchlib.translate.worker")

DEFAULT_LIMIT = 5
MAX_LIMIT = 100
DEFAULT_SLEEP = 60
MIN_SLEEP = 5
MAX_SLEEP = 3600

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


def _blank_state() -> dict[str, Any]:
    return {
        "running": False,
        "source": None,
        "limit": DEFAULT_LIMIT,
        "loop": False,
        "sleep": DEFAULT_SLEEP,
        "provider": "",
        "model": "",
        "started_at": None,
        "finished_at": None,
        "completed_this_run": 0,
        "failed_this_run": 0,
        "skipped_this_run": 0,
        "last_article_id": None,
        "last_status": None,
        "last_reason": None,
        "last_error": None,
        "waiting": False,
        "stop_requested": False,
    }


_state = _blank_state()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _provider_of(settings: Settings) -> str:
    return getattr(settings, "llm_provider", "") or llm_provider()


def status() -> dict[str, Any]:
    """当前状态。只回白名单字段，凭证不在里面。"""
    with _lock:
        snapshot = dict(_state)
        snapshot["thread_alive"] = bool(_thread is not None and _thread.is_alive())
    return snapshot


def _update(**fields: Any) -> None:
    with _lock:
        _state.update(fields)


def _bump(**deltas: int) -> None:
    """计数器在锁里加，读的人不会看到半截。"""
    with _lock:
        for key, delta in deltas.items():
            _state[key] = int(_state.get(key) or 0) + delta


def start(
    settings: Settings,
    *,
    source: str | None = None,
    limit: int = DEFAULT_LIMIT,
    loop: bool = False,
    sleep: int = DEFAULT_SLEEP,
) -> dict[str, Any]:
    """起一个后台线程。已经在跑就不起第二个，只把当前状态带回去。"""
    global _thread
    source_id = (source or "").strip() or None
    limit = _clamp(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    sleep = _clamp(sleep, DEFAULT_SLEEP, MIN_SLEEP, MAX_SLEEP)
    provider = _provider_of(settings)
    with _lock:
        if _thread is not None and _thread.is_alive():
            return {**dict(_state), "thread_alive": True, "started": False}
        _stop.clear()
        _state.clear()
        _state.update(
            _blank_state(),
            running=True,
            source=source_id,
            limit=limit,
            loop=bool(loop),
            sleep=sleep,
            provider=provider_label(provider),
            model=active_model_id(settings),
            started_at=_now(),
        )
        _thread = threading.Thread(
            target=_run,
            args=(settings, source_id, limit, bool(loop), sleep),
            name="translate-worker",
            daemon=True,
        )
        _thread.start()
        return {**dict(_state), "thread_alive": True, "started": True}


def request_stop() -> dict[str, Any]:
    """请 worker 停。正在译的那篇会译完，loop 的休眠会立刻醒。"""
    with _lock:
        running = bool(_state.get("running"))
        if running:
            _stop.set()
            _state["stop_requested"] = True
    return {**status(), "stopped": running}


def credentials_problem(settings: Settings) -> str:
    """触发前先看凭证，别让线程起来才发现没 key。文案和 CLI 一致。"""
    if llm_configured(settings):
        return ""
    if _provider_of(settings) == PROVIDER_GROK:
        return "尚未登录 Grok OAuth。先运行 research-xai-login login。"
    return "没有配置 DEEPSEEK_API_KEY，译制起不来。"


def _connect(settings: Settings):
    """单独开一条连接；worker 线程不能借用请求里的那条。测试里替换掉。"""
    from researchlib.db import connect, ensure_search_session

    conn = connect(settings)
    ensure_search_session(conn)
    return conn


def _run(settings: Settings, source: str | None, limit: int, loop: bool, sleep: int) -> None:
    conn = None
    skipped: set[int] = set()
    fatal = False
    try:
        conn = _connect(settings)
        while not _stop.is_set():
            # limit 加上跳过的数量，免得跳过几篇后这一轮取不满。
            pending = list_pending_english(conn, limit=limit + len(skipped), source_id=source)
            ids = [article_id for article_id in pending if article_id not in skipped][:limit]
            if not ids:
                # 队列空，或者只剩本轮失败/跳过的：loop 时睡一会儿再扫，睡醒清掉跳过再给一次机会。
                if not loop:
                    break
                skipped.clear()
                _update(waiting=True)
                if _stop.wait(sleep):
                    break
                _update(waiting=False)
                continue
            _update(waiting=False)
            for article_id in ids:
                if _stop.is_set():
                    break
                try:
                    result = translate_article(conn, article_id, data_dir=settings.data_dir)
                except XaiLoginRequiredError as exc:
                    conn.rollback()
                    _update(last_article_id=article_id, last_status="failed", last_error=f"需要重新登录：{exc}")
                    fatal = True
                    break
                except Exception as exc:  # noqa: BLE001 - 单篇失败不能带停整个 worker
                    conn.rollback()
                    skipped.add(article_id)
                    _bump(failed_this_run=1)
                    _update(
                        last_article_id=article_id,
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    logger.warning("译制失败 %s：%s", article_id, exc)
                    continue
                if result.status == "skipped":
                    # 没有可译正文的篇目留在队列里，这一轮先跳过，下一轮再看。
                    skipped.add(article_id)
                    _bump(skipped_this_run=1)
                    _update(last_article_id=article_id, last_status=result.status, last_reason=result.reason)
                else:
                    _bump(completed_this_run=1)
                    _update(last_article_id=result.article_id, last_status=result.status, last_error=None)
                logger.info("译制 #%s %s", result.article_id, result.status)
            if fatal or not loop:
                break
    except Exception as exc:  # noqa: BLE001 - 线程里不能把异常抛出去
        logger.exception("译制 worker 出错")
        _update(last_error=str(exc)[:300])
    finally:
        if conn is not None:
            conn.close()
        with _lock:
            _state["running"] = False
            _state["waiting"] = False
            _state["finished_at"] = _now()

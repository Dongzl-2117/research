"""批量任务 worker 共用：状态、单例线程、限幅。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from researchlib.config import Settings
from researchlib.llm import active_model_id, llm_configured
from researchlib.llm_provider import PROVIDER_GROK, llm_provider, provider_label

DEFAULT_LIMIT = 20
MAX_LIMIT = 500
DEFAULT_SLEEP = 60
MIN_SLEEP = 5
MAX_SLEEP = 3600


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def provider_of(settings: Settings) -> str:
    return getattr(settings, "llm_provider", "") or llm_provider()


def credentials_problem(settings: Settings) -> str:
    if llm_configured(settings):
        return ""
    if provider_of(settings) == PROVIDER_GROK:
        return "尚未登录 Grok OAuth。先运行 research-xai-login login。"
    return "没有配置 DEEPSEEK_API_KEY，任务起不来。"


def connect(settings: Settings):
    from researchlib.db import connect as db_connect
    from researchlib.db import ensure_search_session

    conn = db_connect(settings)
    ensure_search_session(conn)
    return conn


class JobWorker:
    """一个任务一个实例：单例线程、白名单状态、可停。"""

    def __init__(self, name: str, *, default_limit: int = DEFAULT_LIMIT):
        self.name = name
        self.default_limit = default_limit
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = self._blank()

    def _blank(self) -> dict[str, Any]:
        return {
            "running": False,
            "kind": self.name,
            "source": None,
            "company": None,
            "limit": self.default_limit,
            "loop": False,
            "sleep": DEFAULT_SLEEP,
            "gap": False,
            "force": False,
            "provider": "",
            "model": "",
            "started_at": None,
            "finished_at": None,
            "completed_this_run": 0,
            "failed_this_run": 0,
            "skipped_this_run": 0,
            "last_id": None,
            "last_label": None,
            "last_status": None,
            "last_error": None,
            "waiting": False,
            "stop_requested": False,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._state)
            snapshot["thread_alive"] = bool(self._thread is not None and self._thread.is_alive())
        return snapshot

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _bump(self, **deltas: int) -> None:
        with self._lock:
            for key, delta in deltas.items():
                self._state[key] = int(self._state.get(key) or 0) + delta

    def request_stop(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._state.get("running"))
            if running:
                self._stop.set()
                self._state["stop_requested"] = True
        return {**self.status(), "stopped": running}

    def start(
        self,
        settings: Settings,
        *,
        run: Callable[[Settings], None],
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {**dict(self._state), "thread_alive": True, "started": False}
            self._stop.clear()
            self._state.clear()
            self._state.update(
                self._blank(),
                running=True,
                provider=provider_label(provider_of(settings)),
                model=active_model_id(settings),
                started_at=now_iso(),
                **(fields or {}),
            )
            self._thread = threading.Thread(
                target=self._wrap,
                args=(settings, run),
                name=f"{self.name}-worker",
                daemon=True,
            )
            self._thread.start()
            return {**dict(self._state), "thread_alive": True, "started": True}

    def _wrap(self, settings: Settings, run: Callable[[Settings], None]) -> None:
        try:
            run(settings)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(f"researchlib.jobs.{self.name}").exception("%s worker 出错", self.name)
            self._update(last_error=str(exc)[:300], last_status="failed")
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["waiting"] = False
                self._state["finished_at"] = now_iso()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

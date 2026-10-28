"""站内论点 worker：HTTP 触发，复用 theses.companies_with_views / needs_build / build。"""

from __future__ import annotations

import logging
from typing import Any

from researchlib.config import Settings
from researchlib.failures import note_failure
from researchlib.jobs.base import (
    DEFAULT_SLEEP,
    MAX_SLEEP,
    MIN_SLEEP,
    JobWorker,
    clamp,
    connect,
)
from researchlib.jobs.base import (
    credentials_problem as shared_credentials_problem,
)
from researchlib.llm import LLMError, LLMUnavailable
from researchlib.sources import enabled_sources, load_sources
from researchlib.theses import build, companies_with_views, needs_build

logger = logging.getLogger("researchlib.jobs.theses")

_worker = JobWorker("theses", default_limit=50)


def status() -> dict[str, Any]:
    return _worker.status()


def request_stop() -> dict[str, Any]:
    return _worker.request_stop()


def credentials_problem(settings: Settings) -> str:
    return shared_credentials_problem(settings)


def start(
    settings: Settings,
    *,
    company: str | None = None,
    loop: bool = False,
    sleep: int = DEFAULT_SLEEP,
    force: bool = False,
) -> dict[str, Any]:
    company_name = (company or "").strip() or None
    sleep = clamp(sleep, DEFAULT_SLEEP, MIN_SLEEP, MAX_SLEEP)
    return _worker.start(
        settings,
        run=lambda s: _run(s, company_name, bool(loop), sleep, bool(force)),
        fields={
            "company": company_name,
            "loop": bool(loop),
            "sleep": sleep,
            "force": bool(force),
            "limit": 0,
        },
    )


def _run(settings: Settings, company: str | None, loop: bool, sleep: int, force: bool) -> None:
    conn = None
    skipped: set[int] = set()
    try:
        conn = connect(settings)
        sources_by_id = {item.id: item for item in enabled_sources(load_sources(settings.sources_path))}
        while not _worker.stop_event.is_set():
            if company:
                row = conn.execute(
                    "SELECT id, name_zh FROM companies WHERE name_zh = %s",
                    (company,),
                ).fetchone()
                if not row:
                    _worker._update(last_status="failed", last_error=f"{company} 不在公司表里")
                    return
                chosen = [row]
            else:
                chosen = companies_with_views(conn, list(sources_by_id))
            batch = [item for item in chosen if int(item["id"]) not in skipped]
            did = 0
            fatal = False
            for item in batch:
                if _worker.stop_event.is_set():
                    break
                company_id = int(item["id"])
                if not force and not needs_build(conn, company_id, sources_by_id, settings):
                    _worker._bump(skipped_this_run=1)
                    continue
                try:
                    count = build(conn, settings, item, sources_by_id)
                except LLMUnavailable as exc:
                    conn.rollback()
                    _worker._update(
                        last_id=company_id,
                        last_label=str(item.get("name_zh") or ""),
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    fatal = True
                    break
                except LLMError as exc:
                    conn.rollback()
                    note_failure(conn, "theses", company_id, exc)
                    skipped.add(company_id)
                    _worker._bump(failed_this_run=1)
                    _worker._update(
                        last_id=company_id,
                        last_label=str(item.get("name_zh") or ""),
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    logger.warning("论点失败 %s：%s", item.get("name_zh"), exc)
                    continue
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    skipped.add(company_id)
                    _worker._bump(failed_this_run=1)
                    _worker._update(
                        last_id=company_id,
                        last_label=str(item.get("name_zh") or ""),
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    logger.warning("论点失败 %s：%s", item.get("name_zh"), exc)
                    continue
                did += 1
                _worker._bump(completed_this_run=1)
                _worker._update(
                    last_id=company_id,
                    last_label=str(item.get("name_zh") or ""),
                    last_status="ok",
                    last_error=None,
                )
                logger.info("论点 %s：%s 条", item.get("name_zh"), count)
            if fatal or not loop:
                break
            # force 只管第一轮：之后和不带 force 一样，只算过期的。否则 loop 会不停地把所有公司重算一遍。
            force = False
            # loop：没有新公司要算时休眠，睡醒清跳过再试
            if did == 0 and not any(
                needs_build(conn, int(item["id"]), sources_by_id, settings)
                for item in chosen
                if int(item["id"]) not in skipped
            ):
                skipped.clear()
                _worker._update(waiting=True)
                if _worker.stop_event.wait(sleep):
                    break
                _worker._update(waiting=False)
    finally:
        if conn is not None:
            conn.close()

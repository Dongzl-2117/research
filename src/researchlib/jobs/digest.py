"""站内摘要 worker：HTTP 触发，复用 digest.pending_articles / digest_article。"""

from __future__ import annotations

import logging
from typing import Any

from researchlib.config import Settings
from researchlib.digest import digest_article, pending_articles
from researchlib.failures import note_failure
from researchlib.jobs.base import (
    DEFAULT_LIMIT,
    DEFAULT_SLEEP,
    MAX_LIMIT,
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

logger = logging.getLogger("researchlib.jobs.digest")

_worker = JobWorker("digest", default_limit=DEFAULT_LIMIT)


def status() -> dict[str, Any]:
    return _worker.status()


def request_stop() -> dict[str, Any]:
    return _worker.request_stop()


def credentials_problem(settings: Settings) -> str:
    return shared_credentials_problem(settings)


def start(
    settings: Settings,
    *,
    source: str | None = None,
    limit: int = DEFAULT_LIMIT,
    loop: bool = False,
    sleep: int = DEFAULT_SLEEP,
) -> dict[str, Any]:
    source_id = (source or "").strip() or None
    limit = clamp(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    sleep = clamp(sleep, DEFAULT_SLEEP, MIN_SLEEP, MAX_SLEEP)
    return _worker.start(
        settings,
        run=lambda s: _run(s, source_id, limit, bool(loop), sleep),
        fields={"source": source_id, "limit": limit, "loop": bool(loop), "sleep": sleep},
    )


def _run(settings: Settings, source: str | None, limit: int, loop: bool, sleep: int) -> None:
    conn = None
    skipped: set[int] = set()
    try:
        conn = connect(settings)
        sources = enabled_sources(load_sources(settings.sources_path))
        if source:
            sources = [item for item in sources if item.id == source]
            if not sources:
                _worker._update(last_status="failed", last_error=f"{source} 不是已启用来源")
                return
        authors = {item.id: item.author for item in sources}
        while not _worker.stop_event.is_set():
            rows = pending_articles(conn, list(authors), limit + len(skipped))
            batch = [row for row in rows if int(row["id"]) not in skipped][:limit]
            if not batch:
                if not loop:
                    break
                skipped.clear()
                _worker._update(waiting=True)
                if _worker.stop_event.wait(sleep):
                    break
                _worker._update(waiting=False)
                continue
            _worker._update(waiting=False)
            fatal = False
            for row in batch:
                if _worker.stop_event.is_set():
                    break
                article_id = int(row["id"])
                try:
                    digest_article(conn, settings, row, authors[row["source_id"]])
                except LLMUnavailable as exc:
                    conn.rollback()
                    _worker._update(
                        last_id=article_id,
                        last_label=str(row.get("title") or "")[:80],
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    fatal = True
                    break
                except LLMError as exc:
                    conn.rollback()
                    note_failure(conn, "digest", article_id, exc)
                    skipped.add(article_id)
                    _worker._bump(failed_this_run=1)
                    _worker._update(
                        last_id=article_id,
                        last_label=str(row.get("title") or "")[:80],
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    logger.warning("摘要失败 %s：%s", article_id, exc)
                    continue
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    skipped.add(article_id)
                    _worker._bump(failed_this_run=1)
                    _worker._update(
                        last_id=article_id,
                        last_label=str(row.get("title") or "")[:80],
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    logger.warning("摘要失败 %s：%s", article_id, exc)
                    continue
                _worker._bump(completed_this_run=1)
                _worker._update(
                    last_id=article_id,
                    last_label=str(row.get("title") or "")[:80],
                    last_status="ok",
                    last_error=None,
                )
                logger.info("摘要 #%s ok", article_id)
            if fatal or not loop:
                break
    finally:
        if conn is not None:
            conn.close()

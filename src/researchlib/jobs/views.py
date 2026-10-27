"""站内观点 worker：HTTP 触发，复用 views.pending_articles / gap_articles / extract_article。"""

from __future__ import annotations

import logging
from typing import Any

from researchlib.config import Settings
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
from researchlib.views import (
    extract_article,
    gap_articles,
    pending_articles,
    pending_companies,
    view_companies,
)

logger = logging.getLogger("researchlib.jobs.views")

_worker = JobWorker("views", default_limit=DEFAULT_LIMIT)


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
    company: str | None = None,
    limit: int = DEFAULT_LIMIT,
    loop: bool = False,
    sleep: int = DEFAULT_SLEEP,
    gap: bool = False,
) -> dict[str, Any]:
    source_id = (source or "").strip() or None
    company_name = (company or "").strip() or None
    limit = clamp(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    sleep = clamp(sleep, DEFAULT_SLEEP, MIN_SLEEP, MAX_SLEEP)
    return _worker.start(
        settings,
        run=lambda s: _run(s, source_id, company_name, limit, bool(loop), sleep, bool(gap)),
        fields={
            "source": source_id,
            "company": company_name,
            "limit": limit,
            "loop": bool(loop),
            "sleep": sleep,
            "gap": bool(gap),
        },
    )


def _run(
    settings: Settings,
    source: str | None,
    company: str | None,
    limit: int,
    loop: bool,
    sleep: int,
    gap: bool,
) -> None:
    conn = None
    skipped: set[int] = set()
    try:
        conn = connect(settings)
        sources = enabled_sources(load_sources(settings.sources_path))
        authors = {item.id: item.author for item in sources}
        if source:
            if source not in authors:
                _worker._update(last_status="failed", last_error=f"{source} 不是已启用来源")
                return
            authors = {source: authors[source]}
        if company:
            row = conn.execute(
                "SELECT id, name_zh FROM companies WHERE name_zh = %s",
                (company,),
            ).fetchone()
            if not row:
                _worker._update(last_status="failed", last_error=f"{company} 不在公司表里")
                return
            wanted = [int(row["id"])]
        elif gap:
            wanted = [int(item["id"]) for item in conn.execute("SELECT id FROM companies").fetchall()]
        else:
            wanted = [int(item["id"]) for item in view_companies(conn, list(authors))]
        if not wanted:
            _worker._update(last_status="failed", last_error="没有被深入谈到的公司")
            return
        while not _worker.stop_event.is_set():
            if gap:
                rows = gap_articles(conn, wanted, list(authors), limit + len(skipped))
            else:
                rows = pending_articles(conn, wanted, list(authors), limit + len(skipped))
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
                if gap:
                    companies = conn.execute(
                        "SELECT id, name_zh FROM companies WHERE id = ANY(%s) ORDER BY id",
                        (list(row["companies"]),),
                    ).fetchall()
                else:
                    companies = pending_companies(conn, article_id)
                try:
                    extract_article(conn, settings, row, authors.get(row["source_id"], ""), companies)
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
                    note_failure(conn, "views", article_id, exc)
                    skipped.add(article_id)
                    _worker._bump(failed_this_run=1)
                    _worker._update(
                        last_id=article_id,
                        last_label=str(row.get("title") or "")[:80],
                        last_status="failed",
                        last_error=str(exc)[:300],
                    )
                    logger.warning("观点失败 %s：%s", article_id, exc)
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
                    logger.warning("观点失败 %s：%s", article_id, exc)
                    continue
                _worker._bump(completed_this_run=1)
                _worker._update(
                    last_id=article_id,
                    last_label=str(row.get("title") or "")[:80],
                    last_status="ok",
                    last_error=None,
                )
                logger.info("观点 #%s ok", article_id)
            if fatal or not loop:
                break
    finally:
        if conn is not None:
            conn.close()

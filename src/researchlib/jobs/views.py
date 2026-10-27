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
from researchlib.mentions import rebuild_articles
from researchlib.views import (
    articles_for_ids,
    extract_article,
    gap_articles,
    pending_articles,
    pending_companies,
    view_companies,
)
from researchlib.views import _canonical_ids as canonical_article_ids

logger = logging.getLogger("researchlib.jobs.views")

_worker = JobWorker("views", default_limit=DEFAULT_LIMIT)


def status() -> dict[str, Any]:
    return _worker.status()


def request_stop() -> dict[str, Any]:
    return _worker.request_stop()


def credentials_problem(settings: Settings) -> str:
    return shared_credentials_problem(settings)


def _parse_article_ids(raw) -> list[int]:
    found: list[int] = []
    if raw is None:
        return found
    if isinstance(raw, (int, float)) and int(raw) > 0:
        return [int(raw)]
    if isinstance(raw, str):
        pieces = raw.replace(",", " ").split()
    elif isinstance(raw, list | tuple):
        pieces = list(raw)
    else:
        return found
    for piece in pieces:
        try:
            value = int(piece)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in found:
            found.append(value)
    return found


def start(
    settings: Settings,
    *,
    source: str | None = None,
    company: str | None = None,
    articles: list[int] | None = None,
    limit: int = DEFAULT_LIMIT,
    loop: bool = False,
    sleep: int = DEFAULT_SLEEP,
    gap: bool = False,
) -> dict[str, Any]:
    source_id = (source or "").strip() or None
    company_name = (company or "").strip() or None
    article_ids = _parse_article_ids(articles)
    limit = clamp(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    sleep = clamp(sleep, DEFAULT_SLEEP, MIN_SLEEP, MAX_SLEEP)
    return _worker.start(
        settings,
        run=lambda s: _run(s, source_id, company_name, article_ids, limit, bool(loop), sleep, bool(gap)),
        fields={
            "source": source_id,
            "company": company_name,
            "articles": article_ids or None,
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
    article_ids: list[int],
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
        # 指定文章时先按当前算法（含短正文 OCR）重挂提及，再抽观点。
        chosen: list[int] = []
        if article_ids:
            rebuilt = rebuild_articles(conn, article_ids)
            logger.info("按文章重挂提及 %s 篇（请求 %s）", rebuilt, article_ids)
            chosen = canonical_article_ids(conn, article_ids, list(authors))
            if not chosen:
                _worker._update(last_status="failed", last_error="指定文章都不在可整理范围内")
                return
        if company:
            row = conn.execute(
                "SELECT id, name_zh FROM companies WHERE name_zh = %s",
                (company,),
            ).fetchone()
            if not row:
                _worker._update(last_status="failed", last_error=f"{company} 不在公司表里")
                return
            wanted = [int(row["id"])]
        elif gap or chosen:
            wanted = [int(item["id"]) for item in conn.execute("SELECT id FROM companies").fetchall()]
        else:
            wanted = [int(item["id"]) for item in view_companies(conn, list(authors))]
        if not wanted:
            _worker._update(last_status="failed", last_error="没有被深入谈到的公司")
            return
        while not _worker.stop_event.is_set():
            if chosen:
                if gap:
                    rows = gap_articles(conn, wanted, list(authors), limit + len(skipped), chosen)
                else:
                    rows = articles_for_ids(conn, wanted, list(authors), chosen, limit + len(skipped))
            elif gap:
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
                if chosen:
                    skipped.add(article_id)
                logger.info("观点 #%s ok", article_id)
            if fatal or not loop:
                break
    finally:
        if conn is not None:
            conn.close()

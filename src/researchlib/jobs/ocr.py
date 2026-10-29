"""站内识图 worker：HTTP 触发，在应用进程里开一个后台线程跑。

和 ``research-ocr`` CLI 用同一套 ``resolve_engine`` / ``run_ocr``。默认 deepseek-flash 识图，
不另开进程，也不在应用启动时自动跑。同一时间只允许一个 worker：已经在跑时再触发只回当前状态。
stop 停在下一张图之前；正在认的那张会认完。

单张失败不回退 RapidOCR。状态里不放凭证。
"""

from __future__ import annotations

import logging
from typing import Any

from researchlib.config import Settings
from researchlib.jobs.base import MAX_LIMIT, JobWorker, clamp, connect

logger = logging.getLogger("researchlib.jobs.ocr")

DEFAULT_LIMIT = 50

_worker = JobWorker("ocr", default_limit=DEFAULT_LIMIT)


def status() -> dict[str, Any]:
    snapshot = _worker.status()
    snapshot.setdefault("engine", None)
    snapshot.setdefault("engine_name", None)
    snapshot.setdefault("redo", False)
    snapshot.setdefault("articles", None)
    snapshot.setdefault("planned", 0)
    snapshot.setdefault("images", 0)
    snapshot.setdefault("with_text", 0)
    snapshot.setdefault("failed", 0)
    snapshot.setdefault("missing", 0)
    snapshot.setdefault("articles_updated", 0)
    return snapshot


def request_stop() -> dict[str, Any]:
    return _worker.request_stop()


def credentials_problem(settings: Settings, engine: str | None = None) -> str:
    from researchlib.ocr import engine_problem

    return engine_problem(settings, engine)


def start(
    settings: Settings,
    *,
    articles: list[int] | None = None,
    redo: bool = False,
    limit: int = DEFAULT_LIMIT,
    engine: str = "vision",
) -> dict[str, Any]:
    from researchlib.ocr import normalize_engine_choice, vision_engine_id

    choice = normalize_engine_choice(engine)
    limit = clamp(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    article_ids = list(articles) if articles else None
    engine_name = vision_engine_id(settings) if choice == "vision" else None
    return _worker.start(
        settings,
        run=lambda current: _run(current, article_ids, bool(redo), limit, choice),
        fields={
            "engine": choice,
            "engine_name": engine_name,
            "redo": bool(redo),
            "limit": limit,
            "articles": article_ids,
            "planned": 0,
            "images": 0,
            "with_text": 0,
            "failed": 0,
            "missing": 0,
            "articles_updated": 0,
        },
    )


def _progress(report: dict) -> None:
    _worker._update(
        images=report.get("images") or 0,
        with_text=report.get("with_text") or 0,
        failed=report.get("failed") or 0,
        missing=report.get("missing") or 0,
        articles_updated=report.get("articles") or 0,
        completed_this_run=report.get("images") or 0,
        failed_this_run=report.get("failed") or 0,
        last_error=report.get("last_error"),
        last_status="running",
    )


def _run(
    settings: Settings,
    article_ids: list[int] | None,
    redo: bool,
    limit: int,
    choice: str,
) -> None:
    from researchlib.ocr import pending_images, resolve_engine, run_ocr

    engine_name, recognize = resolve_engine(settings, choice)
    _worker._update(engine_name=engine_name, last_status="running")
    conn = connect(settings)
    try:
        planned = pending_images(conn, limit, redo=redo, article_ids=article_ids)
        _worker._update(planned=len(planned))
        if _worker.stop_event.is_set():
            _worker._update(last_status="stopped")
            return
        report = run_ocr(
            conn,
            settings.data_dir,
            limit,
            recognize,
            engine=engine_name,
            redo=redo,
            article_ids=article_ids,
            should_stop=_worker.stop_event.is_set,
            on_progress=_progress,
        )
        _worker._update(
            last_status="stopped" if _worker.stop_event.is_set() else "done",
            images=report["images"],
            with_text=report["with_text"],
            failed=report["failed"],
            missing=report["missing"],
            articles_updated=report["articles"],
            completed_this_run=report["images"],
            failed_this_run=report["failed"],
            last_error=report.get("last_error"),
        )
        logger.info(
            "识图 %s 完成：%s 张，失败 %s",
            engine_name,
            report["images"],
            report["failed"],
        )
        follow = report.get("followups") or {}
        if follow.get("digest") or follow.get("zsxq_title"):
            try:
                from researchlib.followups import drain_ocr_followups

                drained = drain_ocr_followups(conn, settings, follow)
                logger.info("识图后续：摘要 %s 篇，标题 %s 篇", drained["digest"], drained["titles"])
            except Exception:
                logger.exception("识图后续没跑完，已入队的留给下次摘要和标题")
    finally:
        conn.close()

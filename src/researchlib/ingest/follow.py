"""导入写完之后，把新文章排进识图和英文译制。

只排这次新写入或改了正文的篇，各有上限。识图已在跑、或没配凭证时记一笔就返回，
不让导入失败。夜间识图仍是兜底。摘要和观点不在这里跑。
"""

from __future__ import annotations

import logging
import os
import sys

from researchlib.translate.language import is_chinese

logger = logging.getLogger("researchlib.ingest.follow")

# 一次导入最多排这么多。增量抓取通常远小于这个；整库回填超出的留在待译队列。
INGEST_TRANSLATE_LIMIT = 20
INGEST_OCR_LIMIT = 40


def follow_enabled() -> bool:
    flag = os.environ.get("RESEARCH_INGEST_FOLLOW", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def plan_followups(rows: list[dict]) -> dict[str, list[int]]:
    """从文章行分出要识图的和要译的。中文不进译制；没挂图片的不进识图。"""
    ocr: list[int] = []
    translate: list[int] = []
    for row in rows:
        article_id = int(row["id"])
        if row.get("has_images"):
            ocr.append(article_id)
        sample = row.get("body_text") or row.get("body_md") or ""
        language = (row.get("raw_meta") or {}).get("language")
        if not is_chinese(sample, language=language if isinstance(language, str) else None):
            translate.append(article_id)
    return {"ocr": ocr, "translate": translate}


def kick_ingest_followups(conn, settings, article_ids: list[int]) -> dict[str, object]:
    """排进现有的识图 / 译制 worker。空列表或关掉开关时什么都不做。"""
    if not article_ids or not follow_enabled():
        return {"ocr": None, "translate": None}
    try:
        return _kick(conn, settings, article_ids)
    except Exception as exc:  # noqa: BLE001 - 导入已经写完，后续排不上不能把这次导入判失败
        logger.exception("入库后续失败")
        print(f"入库后续没排上：{exc}", file=sys.stderr)
        return {"ocr": None, "translate": None, "error": str(exc)[:300]}


def _kick(conn, settings, article_ids: list[int]) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT a.id, a.body_text, a.body_md, a.raw_meta,
               EXISTS (SELECT 1 FROM article_images i WHERE i.article_id = a.id) AS has_images
        FROM articles a
        WHERE a.id = ANY(%s) AND a.duplicate_of IS NULL
        """,
        (list(article_ids),),
    ).fetchall()
    plan = plan_followups(rows)
    ocr_ids = plan["ocr"][:INGEST_OCR_LIMIT]
    translate_ids = plan["translate"][:INGEST_TRANSLATE_LIMIT]
    ocr_note: object = None
    translate_note: object = None
    if ocr_ids:
        ocr_note = _kick_ocr(settings, ocr_ids)
    if translate_ids:
        translate_note = _kick_translate(settings, translate_ids)
    if len(plan["ocr"]) > INGEST_OCR_LIMIT:
        print(
            f"还有 {len(plan['ocr']) - INGEST_OCR_LIMIT} 篇带图文章超过本次识图上限"
            f" {INGEST_OCR_LIMIT}，夜间任务或 POST /api/v1/ocr 续跑",
            file=sys.stderr,
        )
    if len(plan["translate"]) > INGEST_TRANSLATE_LIMIT:
        print(
            f"还有 {len(plan['translate']) - INGEST_TRANSLATE_LIMIT} 篇英文超过本次译制上限"
            f" {INGEST_TRANSLATE_LIMIT}，POST /api/v1/translate 续跑",
            file=sys.stderr,
        )
    return {"ocr": ocr_note, "translate": translate_note}


def _kick_ocr(settings, article_ids: list[int]) -> object:
    from researchlib.jobs import ocr as ocr_job

    problem = ocr_job.credentials_problem(settings, "vision")
    if problem:
        print(f"入库后未排识图：{problem}", file=sys.stderr)
        return {"started": False, "reason": problem}
    result = ocr_job.start(settings, articles=article_ids, limit=INGEST_OCR_LIMIT, engine="vision")
    logger.info("入库识图 %s 篇 started=%s", len(article_ids), result.get("started"))
    print(f"入库后续：识图 {len(article_ids)} 篇（started={result.get('started')}）")
    return result


def _kick_translate(settings, article_ids: list[int]) -> object:
    from researchlib.translate import worker as translate_worker

    problem = translate_worker.credentials_problem(settings)
    if problem:
        print(f"入库后未排译制：{problem}", file=sys.stderr)
        return {"started": False, "reason": problem}
    result = translate_worker.start(
        settings,
        article_ids=article_ids,
        limit=min(len(article_ids), translate_worker.MAX_LIMIT),
        loop=False,
    )
    logger.info("入库译制 %s 篇 started=%s", len(article_ids), result.get("started"))
    print(f"入库后续：译制 {len(article_ids)} 篇（started={result.get('started')}）")
    return result

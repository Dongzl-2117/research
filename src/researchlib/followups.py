"""识图写完后的后续：短正文重排摘要，方伟星球同时重排标题。

触发只看 ocr_hash 变了没有、正文是不是短。不看标题现在像不像壳——
空壳句子以前不会被 title_needs_ai 认出来，排不进去。
真正起题时仍用补过的 title_needs_ai，完好的标题只出队、不打模型。
"""

from __future__ import annotations

import logging

from researchlib.config import Settings
from researchlib.ocr import SHORT_BODY_CHARS
from researchlib.zsxq_title import repairs_source

logger = logging.getLogger("researchlib.followups")

KIND_DIGEST = "digest"
KIND_TITLE = "zsxq_title"
KINDS = frozenset({KIND_DIGEST, KIND_TITLE})
# 同一次识图里立刻重写的上限。剩下的留在队里，下次摘要按这张表优先。
DRAIN_LIMIT = 30


def followup_kinds(*, source_id: str, body_len: int, old_hash: str, new_hash: str) -> list[str]:
    """短正文的 ocr_hash 变了就排摘要；方伟星球再排标题。不看标题文本。"""
    new = new_hash or ""
    old = old_hash or ""
    if not new or new == old:
        return []
    if int(body_len or 0) >= SHORT_BODY_CHARS:
        return []
    kinds = [KIND_DIGEST]
    if repairs_source(source_id or ""):
        kinds.append(KIND_TITLE)
    return kinds


def enqueue(conn, article_id: int, kind: str, reason: str) -> None:
    if kind not in KINDS:
        raise ValueError(kind)
    conn.execute(
        """
        INSERT INTO article_followups (article_id, kind, reason)
        VALUES (%s, %s, %s)
        ON CONFLICT (article_id, kind) DO UPDATE
          SET reason = EXCLUDED.reason, created_at = now()
        """,
        (int(article_id), kind, (reason or "")[:80]),
    )


def clear_followup(conn, article_id: int, kind: str) -> None:
    conn.execute(
        "DELETE FROM article_followups WHERE article_id = %s AND kind = %s",
        (int(article_id), kind),
    )


def queued_ids(conn, kind: str, limit: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT article_id FROM article_followups
        WHERE kind = %s
        ORDER BY created_at, article_id
        LIMIT %s
        """,
        (kind, max(int(limit), 1)),
    ).fetchall()
    return [int(row["article_id"]) for row in rows]


def note_ocr_changes(conn, rows: list[dict]) -> dict[str, list[int]]:
    """把这次哈希变了的短正文写进队。不提交。"""
    found = {KIND_DIGEST: [], KIND_TITLE: []}
    for row in rows:
        kinds = followup_kinds(
            source_id=row.get("source_id") or "",
            body_len=int(row.get("body_len") or 0),
            old_hash=row.get("old_hash") or "",
            new_hash=row.get("new_hash") or "",
        )
        article_id = int(row["id"])
        for kind in kinds:
            enqueue(conn, article_id, kind, "ocr_hash")
            found[kind].append(article_id)
    return found


def drain_ocr_followups(conn, settings: Settings, followups: dict, *, limit: int = DRAIN_LIMIT) -> dict:
    """识图进程退出前，把刚入队的短正文重写掉。没凭证就留在队里。

    只处理这次入队的 id，不扫缺摘要的积压。
    """
    from researchlib.llm import llm_configured

    digest_ids = [int(item) for item in (followups.get(KIND_DIGEST) or [])][:limit]
    title_ids = [int(item) for item in (followups.get(KIND_TITLE) or [])][:limit]
    done = {"digest": 0, "titles": 0}
    if not digest_ids and not title_ids:
        return done
    if not llm_configured(settings):
        logger.info("没有 LLM 凭证，摘要 %s 篇、标题 %s 篇留在队里", len(digest_ids), len(title_ids))
        return done
    if digest_ids:
        done["digest"] = _drain_digests(conn, settings, digest_ids)
    if title_ids:
        done["titles"] = _drain_titles(conn, settings, title_ids)
    return done


def _drain_digests(conn, settings: Settings, article_ids: list[int]) -> int:
    from researchlib.digest import digest_article, rows_needing_digest
    from researchlib.llm import LLMError, LLMUnavailable
    from researchlib.sources import enabled_sources, load_sources

    authors = {item.id: item.author for item in enabled_sources(load_sources(settings.sources_path))}
    done = 0
    for row in rows_needing_digest(conn, article_ids):
        try:
            digest_article(conn, settings, row, authors.get(row["source_id"], ""))
            done += 1
        except LLMUnavailable:
            logger.warning("摘要后续暂停，剩下的留在队里")
            conn.rollback()
            break
        except LLMError:
            logger.warning("摘要后续失败 %s", row["id"])
            conn.rollback()
        except Exception:
            logger.exception("摘要后续失败 %s", row["id"])
            conn.rollback()
    return done


def _drain_titles(conn, settings: Settings, article_ids: list[int]) -> int:
    from researchlib.zsxq_repair import repair_zsxq

    stats = repair_zsxq(
        conn,
        article_ids=article_ids,
        ai_titles=True,
        titles_only=True,
        settings=settings,
        limit=len(article_ids),
    )
    conn.commit()
    return int(stats.get("titles") or 0)

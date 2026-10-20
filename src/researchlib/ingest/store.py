"""把解析结果写入数据库。以 (source_id, external_id) 幂等。"""

from __future__ import annotations

import json
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.chunks import replace_chunks, replace_ocr_chunks
from researchlib.duplicates import resolve_article
from researchlib.images import sync_article_images
from researchlib.ingest.model import IngestReport, ParsedDoc
from researchlib.mentions import (
    CompanyMatcher,
    load_company_rows,
    mentions_for_document,
    replace_mentions,
)
from researchlib.revisions import held_before, judge_replacement, save_revision
from researchlib.sources import Source
from researchlib.textutil import content_hash, markdown_to_text, split_frontmatter, strip_duplicate_title


def _body_text(title: str, body_md: str) -> str:
    _frontmatter, body = split_frontmatter(body_md)
    return markdown_to_text(strip_duplicate_title(body, title))


def store_documents(
    conn,
    *,
    source: Source,
    docs: list[ParsedDoc],
    data_dir: Path,
    archive_files: int,
    images_in_archive: int,
    errors: list[str],
    rejected: int = 0,
    origin: str = "archive",
) -> IngestReport:
    """origin：archive（手动导入的归档）或 aihot（从 AIHOT 导入，定时任务走这条）。首页断更提示只看后者。"""
    report = IngestReport(
        source_id=source.id,
        source_name=source.name,
        archive_files=archive_files,
        images_in_archive=images_in_archive,
        errors=list(errors),
        rejected=rejected,
    )
    matcher = CompanyMatcher(load_company_rows(conn))
    for doc in docs:
        try:
            status, stored, linked, missing, duplicate = _store_one(conn, source, doc, data_dir, matcher)
            conn.commit()
        except Exception as exc:
            report.errors.append(f"{doc.external_id}: {exc}")
            conn.rollback()
            continue
        if duplicate is not None:
            report.duplicates += 1
        if status == "inserted":
            report.inserted += 1
        elif status == "updated":
            report.updated += 1
        elif status == "held":
            report.held += 1
        else:
            report.unchanged += 1
        report.images_stored += stored
        report.images_linked += linked
        report.images_missing += missing
    row = conn.execute(
        "SELECT count(*) AS n FROM articles WHERE source_id = %s",
        (source.id,),
    ).fetchone()
    report.db_rows = int(row["n"])
    record_import(conn, source.id, report, origin)
    return report


IMPORT_KEY = "import:"


def record_import(conn, source_id: str, report: IngestReport, origin: str = "archive") -> None:
    """记下这个来源最近一次导入的时间、计数和走的哪条路。首页判断「是不是断更了」时一并显示。"""
    payload = {
        "origin": origin,
        "inserted": report.inserted,
        "updated": report.updated,
        "unchanged": report.unchanged,
        "held": report.held,
        "duplicates": report.duplicates,
        "errors": len(report.errors),
    }
    conn.execute(
        """
        INSERT INTO app_state (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (IMPORT_KEY + source_id, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


def _store_one(conn, source: Source, doc: ParsedDoc, data_dir: Path, matcher: CompanyMatcher):
    body_text = _body_text(doc.title, doc.body_md)
    digest = content_hash(doc.title, doc.body_md, doc.published_at)
    meta = dict(doc.raw_meta)
    existing = conn.execute(
        """
        SELECT id, content_hash, title, published_at, body_md, body_text, raw_meta
        FROM articles
        WHERE source_id = %s AND external_id = %s
        """,
        (source.id, doc.external_id),
    ).fetchone()
    if existing is not None and existing["content_hash"] != digest:
        held = _hold_if_suspicious(conn, existing, doc, digest, body_text, meta)
        if held:
            # 库里的完整原文和它的图片都不动。
            return "held", 0, 0, 0, None
    if existing is None:
        inserted = conn.execute(
            """
            INSERT INTO articles (
              source_id, external_id, content_hash, title, published_at, body_md, body_text, raw_meta
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                source.id,
                doc.external_id,
                digest,
                doc.title,
                doc.published_at,
                doc.body_md,
                body_text,
                Jsonb(meta),
            ),
        ).fetchone()
        article_id = inserted["id"]
        status = "inserted"
    elif existing["content_hash"] != digest:
        if existing["body_md"] != doc.body_md or existing["title"] != doc.title:
            # 替换前把旧版存下来。只改了发布时间的不算改稿。
            save_revision(
                conn,
                existing["id"],
                kind="previous",
                content_hash=existing["content_hash"],
                title=existing["title"],
                published_at=existing["published_at"],
                body_md=existing["body_md"],
                body_text=existing["body_text"],
                raw_meta=existing["raw_meta"],
            )
        conn.execute(
            """
            UPDATE articles SET
              content_hash = %s,
              title = %s,
              published_at = %s,
              body_md = %s,
              body_text = %s,
              raw_meta = %s,
              updated_at = now()
            WHERE id = %s
            """,
            (digest, doc.title, doc.published_at, doc.body_md, body_text, Jsonb(meta), existing["id"]),
        )
        article_id = existing["id"]
        status = "updated"
    else:
        article_id = existing["id"]
        status = "unchanged"
    if status != "unchanged":
        replace_mentions(conn, article_id, mentions_for_document(matcher, doc.title, doc.body_md))
        replace_chunks(conn, article_id, doc.title, doc.body_md)
    stored, linked, missing = sync_article_images(
        conn,
        article_id=article_id,
        source_id=source.id,
        data_dir=data_dir,
        refs=doc.image_refs,
    )
    duplicate = None
    if status != "unchanged":
        # 图片可能换了：图片文字（OCR）的块跟着重切，识别结果按图片哈希缓存，不用重新识别。
        replace_ocr_chunks(conn, article_id)
        # 图片挂上之后再认重复：正文更长的优先，图片作次要依据。
        duplicate = resolve_article(conn, article_id, matcher)
        if duplicate is None:
            # 这篇成了主版本，其它篇被标成重复时也算认出一次。
            pointed = conn.execute(
                "SELECT count(*) AS n FROM articles WHERE duplicate_of = %s", (article_id,)
            ).fetchone()["n"]
            if pointed:
                duplicate = article_id
    return status, stored, linked, missing, duplicate


def _hold_if_suspicious(conn, existing, doc: ParsedDoc, digest: str, body_text: str, meta: dict) -> bool:
    """新正文像删除提示、或比库里短一半以上：不替换，把这一版记成 held。返回是否拦下了。"""
    if held_before(conn, existing["id"], digest):
        return True
    reason = judge_replacement(existing["body_text"], body_text)
    if not reason:
        return False
    save_revision(
        conn,
        existing["id"],
        kind="held",
        reason=reason,
        content_hash=digest,
        title=doc.title,
        published_at=doc.published_at,
        body_md=doc.body_md,
        body_text=body_text,
        raw_meta=meta,
    )
    return True

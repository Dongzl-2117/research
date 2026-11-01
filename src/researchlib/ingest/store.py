"""把解析结果写入数据库。以 (source_id, external_id) 幂等。"""

from __future__ import annotations

import json
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.author_voice import assign_poster_role, is_guest
from researchlib.boilerplate import EMPTY, Profile, clean_markdown, image_hashes, profile_for, search_text
from researchlib.chunks import replace_chunks, replace_ocr_chunks
from researchlib.duplicates import resolve_article
from researchlib.images import sync_article_images
from researchlib.ingest.model import IngestReport, ParsedDoc
from researchlib.mentions import (
    CompanyMatcher,
    body_for_mentions,
    load_company_rows,
    mentions_for_document,
    replace_mentions,
)
from researchlib.revisions import article_text, held_before, judge_replacement, save_revision, split_comments
from researchlib.sources import Source
from researchlib.textutil import content_hash
from researchlib.visibility import HIDDEN, SHORT_POST
from researchlib.zsxq_title import repairs_source, title_needs_repair


def _body_text(title: str, body_md: str, profile: Profile = EMPTY, hashes: dict[str, str] | None = None) -> str:
    """检索、摘要、观点读的文字：去掉归档头、引流尾巴和广告图之后的正文。"""
    return search_text(clean_markdown(body_md, profile, hashes), title)


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
    """origin：archive（手动导入的归档）或 crawl（定时抓取脚本导入）。以前从 AIHOT 导入的记作 aihot。

    首页断更提示只看 crawl。
    """
    report = IngestReport(
        source_id=source.id,
        source_name=source.name,
        archive_files=archive_files,
        images_in_archive=images_in_archive,
        errors=list(errors),
        rejected=rejected,
    )
    if source.kind == "zsxq":
        report.removed_index = forget_index_articles(conn, source.id)
        if report.removed_index:
            conn.commit()
    matcher = CompanyMatcher(load_company_rows(conn))
    for doc in docs:
        try:
            status, article_id, stored, linked, missing, duplicate = _store_one(
                conn, source, doc, data_dir, matcher
            )
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
_INDEX_NAMES = frozenset({"index.md", "index.markdown"})
_INDEX_IDS = frozenset({"index", "index.md", "index.markdown", "md/index.md"})


def forget_index_articles(conn, source_id: str) -> int:
    """INDEX.md 是归档目录，不是一条话题。从库里去掉，避免假日期进按天。"""
    rows = conn.execute(
        """
        DELETE FROM articles
        WHERE source_id = %s
          AND (
            lower(coalesce(raw_meta->>'filename', '')) = ANY(%s)
            OR lower(external_id) = ANY(%s)
          )
        RETURNING id
        """,
        (source_id, list(_INDEX_NAMES), list(_INDEX_IDS)),
    ).fetchall()
    return len(rows)


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


def body_chars(body_md: str) -> int:
    """作者正文的字数：去掉 YAML 头、评论一节和所有空白。"""
    import re

    article, _ = split_comments(body_md or "")
    article = re.sub(r"\A---\n.*?\n---\n", "", article, flags=re.S)
    return len("".join(article.split()))


def apply_length_floor(source: Source, body_md: str, meta: dict) -> dict:
    """短于来源 min_chars 的帖子标 hidden=short_post；够长则去掉这个标记。"""
    if source.min_chars <= 0:
        return meta
    if body_chars(body_md) < source.min_chars:
        meta[HIDDEN] = SHORT_POST
    elif meta.get(HIDDEN) == SHORT_POST:
        meta.pop(HIDDEN)
    return meta


def _keeps_repaired_title(source: Source, existing, doc: ParsedDoc) -> bool:
    """星球帖重导：导入算出的标题还要补（只有图的「「图片」」），库里的已经补好了，正文也没改。

    导入时没有图片里的字和摘要，这种标题补不出来；夜间回写（含 flash 起题）补好以后，
    星球抓取每次整包重导都会把它改回占位，回写再为同一批帖子打 flash。这时留库里的。
    """
    stored = existing["title"] or ""
    if not stored or stored == doc.title or not repairs_source(source.id, source.kind):
        return False
    if not title_needs_repair(doc.title, doc.body_md) or title_needs_repair(stored, doc.body_md):
        return False
    return article_text(existing["body_md"]) == article_text(doc.body_md)


def _store_one(conn, source: Source, doc: ParsedDoc, data_dir: Path, matcher: CompanyMatcher):
    profile = profile_for(conn, source.id)
    existing = conn.execute(
        """
        SELECT id, content_hash, title, published_at, body_md, body_text, raw_meta
        FROM articles
        WHERE source_id = %s AND external_id = %s
        """,
        (source.id, doc.external_id),
    ).fetchone()
    if existing is not None and _keeps_repaired_title(source, existing, doc):
        doc.title = existing["title"]
    body_text = _body_text(doc.title, doc.body_md, profile)
    digest = content_hash(doc.title, doc.body_md, doc.published_at)
    poster = assign_poster_role(dict(doc.raw_meta), source.author, source.poster_names)
    meta = apply_length_floor(source, doc.body_md, poster)
    keep = False
    article_changed = True
    same_words = False
    if existing is not None and existing["content_hash"] != digest:
        old_article, old_comments = split_comments(existing["body_md"])
        new_article, new_comments = split_comments(doc.body_md)
        if (old_article == new_article and existing["title"] == doc.title) or (
            article_text(existing["body_md"]) == article_text(doc.body_md)
        ):
            # 作者写的正文没变：只是接在后面的「评论里的回复」变了，或者归档头里的点赞、阅读数变了、
            # 标题换了补法。不算改稿。回复少了（这次导出没带评论，或者回复被删了）时库里那份更全，不动。
            article_changed = False
            keep = len(new_comments) < len(old_comments)
            same_words = new_comments == old_comments
        else:
            held = _hold_if_suspicious(
                conn,
                existing,
                doc,
                digest,
                old_text=_body_text(existing["title"], old_article),
                new_text=_body_text(doc.title, new_article),
                body_text=body_text,
                meta=meta,
            )
            if held:
                # 库里的完整原文和它的图片都不动。
                return "held", existing["id"], 0, 0, 0, None
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
    elif existing["content_hash"] != digest and not keep:
        if article_changed and (existing["body_md"] != doc.body_md or existing["title"] != doc.title):
            # 替换前把旧版存下来。只改了发布时间、只多了评论里的回复的不算改稿。
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
        if same_words:
            # 只是归档头里的点赞、阅读数，补出来的标题，或发布时间变了：观点、摘要和译文说的还是这篇，
            # 哈希跟过来，摘要不会从卡片上消失，夜里也不用重抽。评论回复变了的照旧重抽。
            from researchlib.retime import _realign

            _realign(conn, existing["id"], {existing["content_hash"]}, digest)
        article_id = existing["id"]
        status = "updated"
    else:
        article_id = existing["id"]
        status = "unchanged"
        # 正文没变也要补上发帖人：历史归档重导一遍就能把网友帖从星主名下摘掉。
        if doc.raw_meta.get("poster_name") or doc.raw_meta.get("poster_id"):
            merged = assign_poster_role(dict(existing["raw_meta"] or {}), source.author, source.poster_names)
            merged["poster_name"] = str(doc.raw_meta.get("poster_name") or "")
            merged["poster_id"] = str(doc.raw_meta.get("poster_id") or "")
            assign_poster_role(merged, source.author, source.poster_names)
            if merged != dict(existing["raw_meta"] or {}):
                conn.execute(
                    "UPDATE articles SET raw_meta = %s WHERE id = %s",
                    (Jsonb(merged), article_id),
                )
                if is_guest(merged):
                    conn.execute("DELETE FROM company_views WHERE article_id = %s", (article_id,))
    if status != "unchanged":
        # 入库时 OCR 多半还空；若短正文已有旁路（重抓保图），一并扫进提及。
        ocr_text = ""
        stored = conn.execute(
            "SELECT ocr_text FROM articles WHERE id = %s",
            (article_id,),
        ).fetchone()
        if stored is not None:
            ocr_text = stored["ocr_text"] or ""
        mention_body = body_for_mentions(doc.body_md, body_text=body_text, ocr_text=ocr_text)
        replace_mentions(conn, article_id, mentions_for_document(matcher, doc.title, mention_body))
        replace_chunks(conn, article_id, doc.title, doc.body_md)
    stored, linked, missing = sync_article_images(
        conn,
        article_id=article_id,
        source_id=source.id,
        data_dir=data_dir,
        refs=doc.image_refs,
    )
    if status != "unchanged":
        # 图片挂上以后才知道哪张是广告图：按图片哈希再洗一遍文字和块。
        hashes = image_hashes(conn, article_id)
        cleaned = _body_text(doc.title, doc.body_md, profile, hashes)
        if cleaned != body_text:
            conn.execute("UPDATE articles SET body_text = %s WHERE id = %s", (cleaned, article_id))
            replace_chunks(conn, article_id, doc.title, doc.body_md)
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
    if is_guest(meta):
        conn.execute("DELETE FROM company_views WHERE article_id = %s", (article_id,))
    return status, article_id, stored, linked, missing, duplicate


def _hold_if_suspicious(
    conn, existing, doc: ParsedDoc, digest: str, *, old_text: str, new_text: str, body_text: str, meta: dict
) -> bool:
    """新正文像删除提示、或比库里短一半以上：不替换，把这一版记成 held。返回是否拦下了。

    old_text / new_text 只是作者写的正文（不含「评论里的回复」），评论多少不影响判断。
    """
    if held_before(conn, existing["id"], digest):
        return True
    reason = judge_replacement(old_text, new_text)
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

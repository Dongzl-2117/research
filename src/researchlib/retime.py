"""用归档里的真实 create_time 更正知识星球发布时间，并按新时间重排观点。

不访问知识星球接口，也不编造时间：只有 raw JSON 的 create_time，或正文开头「- 时间：」
带钟点时才改。INDEX.md 不是话题，从库里去掉。

库里的 external_id 多半是 ``YYYY-MM-DD_<topic_id>``，也有纯数字。按最后一个 ``_`` 后面的
话题 id（或整段就是数字）去对 ``raw/topics/<id>.json``，不改外部 id 本身。

观点时间线和「同期别人怎么看」按 articles.published_at 现算。以前全是当天零点，同一天里的
先后只能靠入库顺序，态度变化会标在错误的那篇上。这里改完钟点后把每家公司的态度变化重走一遍，
并用同一条 ±30 天窗口重数同期。正文没变时，把观点、摘要、译文的 content_hash 对齐到新时间，
避免夜间任务把「只改了钟点」当成正文变了、按旧理解再抽一遍。

不给 ``--input`` 时不读归档，用库里存的正文开头「- 时间：」，规则和导入时一样，只看库里还没有
真实钟点的那些篇。

    research-retime --source fangwei-zsxq --input /data/inbox/fangwei-zsxq
    research-retime --source fangwei-zsxq --input /data/inbox/fangwei-zsxq --dry-run
    research-retime --source chengfengpolang --dry-run
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.config import Settings
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import forget_index_articles
from researchlib.sources import Source
from researchlib.textutil import (
    SHANGHAI,
    content_hash,
    date_from_filename,
    resolve_published_at,
    split_frontmatter,
)

_INDEX_IDS = frozenset({"index", "index.md", "index.markdown"})


def topic_id_from_external_id(external_id: str) -> str | None:
    """纯数字，或最后一个 ``_`` 后面的数字。``YYYY-MM-DD_<topic_id>`` 取话题 id。INDEX 不算。"""
    text = (external_id or "").strip().replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    lower = text.lower()
    if lower in _INDEX_IDS:
        return None
    if lower.endswith(".markdown"):
        text = text[: -len(".markdown")]
    elif lower.endswith(".md"):
        text = text[: -len(".md")]
    if text.isdigit():
        return text
    suffix = text.rsplit("_", 1)[-1]
    if suffix.isdigit() and suffix != text:
        return suffix
    return None


def _doc_topic_id(doc: ParsedDoc) -> str | None:
    raw = doc.raw_meta.get("topic_id")
    if raw not in (None, ""):
        text = str(raw).strip()
        if text.isdigit():
            return text
    return topic_id_from_external_id(doc.external_id)


def docs_from_library(conn, source_id: str) -> list[ParsedDoc]:
    """没有归档时，从库里存的正文重新取发布时间，和导入走同一个 resolve_published_at。

    只看库里还是当天零点（或没有日期）、又没标 time_known 的。正文开头也没有带钟点的「- 时间：」的，
    time_known 仍是 false，refresh_clocks 会跳过，不编造时间。
    """
    rows = conn.execute(
        "SELECT external_id, title, body_md, published_at, raw_meta FROM articles WHERE source_id = %s",
        (source_id,),
    ).fetchall()
    docs = []
    for row in rows:
        meta = dict(row["raw_meta"] or {})
        if meta.get("time_known") is True:
            continue
        stored_at = row["published_at"]
        if stored_at is not None:
            local = stored_at.astimezone(SHANGHAI)
            if (local.hour, local.minute, local.second) != (0, 0, 0):
                continue
        body = row["body_md"] or ""
        frontmatter, _rest = split_frontmatter(body)
        when, date_source, known = resolve_published_at(
            frontmatter=frontmatter,
            hints=meta,
            filename=str(meta.get("filename") or ""),
            text=body,
            extra_filenames=(str(row["external_id"] or ""),),
        )
        docs.append(
            ParsedDoc(
                external_id=row["external_id"],
                title=row["title"],
                published_at=when,
                body_md=body,
                raw_meta={"date_source": date_source, "time_known": known},
                image_refs=[],
            )
        )
    return docs


def _same_instant(left, right) -> bool:
    if left is None or right is None:
        return False
    return left.astimezone(SHANGHAI) == right.astimezone(SHANGHAI)


def _clock_hashes(title: str, body: str, published_at, meta: dict, external_id: str) -> set[str]:
    """正文不变、只是零点换成钟点时，观点上可能还留着的旧 content_hash。"""
    moments = []
    if published_at is not None:
        local = published_at.astimezone(SHANGHAI)
        for delta in (-1, 0, 1):
            moments.append((local + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0))
    for name in (str((meta or {}).get("filename") or ""), external_id or ""):
        named = date_from_filename(name)
        if named is not None:
            moments.append(named)
    return {content_hash(title, body, moment) for moment in moments}


def _realign(conn, article_id: int, old_hashes: set[str], target: str) -> dict[str, int]:
    old = [item for item in old_hashes if item and item != target]
    counts = {"views": 0, "digests": 0, "translations": 0, "fingerprints": 0}
    if not old:
        return counts
    counts["views"] = conn.execute(
        """
        UPDATE company_views SET content_hash = %s
        WHERE article_id = %s AND content_hash = ANY(%s) AND content_hash <> %s
        """,
        (target, article_id, old, target),
    ).rowcount
    counts["digests"] = conn.execute(
        """
        UPDATE article_digests SET content_hash = %s
        WHERE article_id = %s AND content_hash = ANY(%s) AND content_hash <> %s
        """,
        (target, article_id, old, target),
    ).rowcount
    counts["translations"] = conn.execute(
        """
        UPDATE article_translations SET content_hash = %s
        WHERE article_id = %s AND content_hash = ANY(%s) AND content_hash <> %s
        """,
        (target, article_id, old, target),
    ).rowcount
    counts["fingerprints"] = conn.execute(
        """
        UPDATE articles SET fingerprint_of = %s
        WHERE id = %s AND fingerprint_of = ANY(%s) AND fingerprint_of IS DISTINCT FROM %s
        """,
        (target, article_id, old, target),
    ).rowcount
    return counts


def _merge_meta(existing: dict, doc: ParsedDoc) -> dict:
    meta = dict(existing or {})
    create_time = doc.raw_meta.get("create_time")
    if create_time not in (None, ""):
        meta["create_time"] = create_time if isinstance(create_time, (str, int, float, bool)) else str(create_time)
    if doc.raw_meta.get("date_source"):
        meta["date_source"] = doc.raw_meta["date_source"]
    topic_id = doc.raw_meta.get("topic_id")
    if topic_id not in (None, ""):
        meta["topic_id"] = topic_id if isinstance(topic_id, (str, int)) else str(topic_id)
    meta["time_known"] = True
    return meta


def companies_touched(conn, source_id: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT v.company_id
        FROM company_views v
        JOIN articles a ON a.id = v.article_id
        WHERE a.source_id = %s AND v.judgment <> '' AND a.duplicate_of IS NULL
        ORDER BY v.company_id
        """,
        (source_id,),
    ).fetchall()
    return [int(row["company_id"]) for row in rows]


def stance_changes(conn, settings: Settings, company_ids: list[int], sources_by_id: dict) -> list[dict]:
    """按当前 published_at 把态度变化重走一遍。和公司页用的是同一个 timeline()。"""
    from researchlib.views import timeline

    found = []
    for company_id in company_ids:
        for item in timeline(conn, company_id, sources_by_id, settings):
            if item["changed"]:
                found.append(
                    {
                        "company_id": company_id,
                        "article_id": item["article_id"],
                        "author": item["author"],
                        "previous": item["previous_label"],
                        "stance": item["stance_label"],
                    }
                )
    return found


def count_same_period(
    conn, source_id: str, source_ids: list[str], tracked_ids: list[int] | None, days: int = 30
) -> int:
    """多少篇这个来源的文章，在 ±days 天里能看到别的作者的判断。和文章页同一窗口。"""
    if not source_ids or tracked_ids == []:
        return 0
    company_sql = ""
    params: dict = {"source": source_id, "sources": source_ids, "days": days}
    if tracked_ids:
        company_sql = "AND m.company_id = ANY(%(tracked)s)"
        params["tracked"] = tracked_ids
    row = conn.execute(
        f"""
        SELECT count(DISTINCT me.id) AS n
        FROM articles me
        JOIN sources me_src ON me_src.id = me.source_id
        JOIN article_mentions m ON m.article_id = me.id AND m.depth <> 'passing' {company_sql}
        JOIN company_views v ON v.company_id = m.company_id AND v.judgment <> ''
        JOIN articles a ON a.id = v.article_id
        JOIN sources other ON other.id = a.source_id
        WHERE me.source_id = %(source)s AND me.duplicate_of IS NULL AND me.published_at IS NOT NULL
          AND a.id <> me.id AND a.duplicate_of IS NULL AND a.source_id = ANY(%(sources)s)
          AND other.author <> me_src.author
          AND a.published_at BETWEEN me.published_at - make_interval(days => %(days)s)
                                 AND me.published_at + make_interval(days => %(days)s)
        """,
        params,
    ).fetchone()
    return int(row["n"])


def _tracked_ids(conn, settings: Settings) -> list[int] | None:
    """同期窗口不再按持仓过滤，传 None。"""
    return None


def refresh_clocks(
    conn,
    source: Source,
    docs: list[ParsedDoc],
    settings: Settings,
    *,
    dry_run: bool = False,
    same_row: bool = False,
) -> dict:
    """改 published_at 和 raw_meta.create_time，对齐哈希，再按新时间重走态度变化和同期。

    归档里的一篇按话题号对上库里同一话题的所有行。same_row 用于 docs_from_library 逐行生成的 docs：
    只改外部 id 相同的那一行，不把一行正文里的钟点连带写到同话题的别的行上。
    """
    from researchlib.sources import enabled_sources, load_sources

    sources_by_id = {item.id: item for item in enabled_sources(load_sources(settings.sources_path))}
    touched = companies_touched(conn, source.id)
    before_changes = stance_changes(conn, settings, touched, sources_by_id)
    before_period = count_same_period(conn, source.id, list(sources_by_id), _tracked_ids(conn, settings))
    stats = {
        "source_id": source.id,
        "updated": 0,
        "unchanged": 0,
        "skipped_no_clock": 0,
        "missing": 0,
        "removed_index": 0,
        "views": 0,
        "digests": 0,
        "translations": 0,
        "fingerprints": 0,
        "before_changes": len(before_changes),
        "after_changes": len(before_changes),
        "before_period": before_period,
        "after_period": before_period,
        "changes": before_changes,
        "dry_run": dry_run,
    }
    try:
        stats["removed_index"] = forget_index_articles(conn, source.id)
        stored = conn.execute(
            """
            SELECT id, title, body_md, published_at, content_hash, raw_meta, external_id
            FROM articles
            WHERE source_id = %s
            """,
            (source.id,),
        ).fetchall()
        by_topic: dict[str, list] = {}
        by_external: dict[str, list] = {}
        for row in stored:
            by_external.setdefault(row["external_id"], []).append(row)
            key = topic_id_from_external_id(row["external_id"])
            if key:
                by_topic.setdefault(key, []).append(row)
        seen: set[int] = set()
        for doc in docs:
            if not doc.raw_meta.get("time_known") or doc.published_at is None:
                stats["skipped_no_clock"] += 1
                continue
            if same_row:
                candidates = by_external.get(doc.external_id, [])
            else:
                candidates = by_topic.get(_doc_topic_id(doc) or "", [])
            matches = [row for row in candidates if row["id"] not in seen]
            if not matches:
                stats["missing"] += 1
                continue
            for existing in matches:
                seen.add(existing["id"])
                _apply_clock(conn, existing, doc, stats)
        touched = companies_touched(conn, source.id)
        after_changes = stance_changes(conn, settings, touched, sources_by_id)
        stats["after_changes"] = len(after_changes)
        stats["changes"] = after_changes
        stats["after_period"] = count_same_period(conn, source.id, list(sources_by_id), _tracked_ids(conn, settings))
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def _apply_clock(conn, existing, doc: ParsedDoc, stats: dict) -> None:
    meta = _merge_meta(existing["raw_meta"] or {}, doc)
    title = existing["title"]
    body = existing["body_md"]
    target = existing["content_hash"]
    changed_clock = not _same_instant(existing["published_at"], doc.published_at)
    changed_meta = meta != dict(existing["raw_meta"] or {})
    if changed_clock:
        target = content_hash(title, body, doc.published_at)
        conn.execute(
            """
            UPDATE articles
            SET published_at = %s, content_hash = %s, raw_meta = %s, updated_at = now()
            WHERE id = %s
            """,
            (doc.published_at, target, Jsonb(meta), existing["id"]),
        )
        stats["updated"] += 1
    elif changed_meta:
        conn.execute(
            "UPDATE articles SET raw_meta = %s, updated_at = now() WHERE id = %s",
            (Jsonb(meta), existing["id"]),
        )
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1
    old_hashes = _clock_hashes(
        title, body, doc.published_at, existing["raw_meta"] or {}, existing["external_id"]
    )
    old_hashes.add(existing["content_hash"])
    aligned = _realign(conn, existing["id"], old_hashes, target)
    for key in ("views", "digests", "translations", "fingerprints"):
        stats[key] += aligned[key]


def render_stats(stats: dict) -> str:
    lines = [
        f"来源: {stats['source_id']}" + ("（演练，未写入）" if stats["dry_run"] else ""),
        f"更正发布时间: {stats['updated']}",
        f"时间未改: {stats['unchanged']}",
        f"没有真实钟点，保持原样: {stats['skipped_no_clock']}",
        f"库里没有这篇: {stats['missing']}",
        f"去掉 INDEX: {stats['removed_index']}",
        f"观点对齐: {stats['views']}",
        f"摘要对齐: {stats['digests']}",
        f"译文对齐: {stats['translations']}",
        (
            f"态度变化: {stats['before_changes']} 处 → {stats['after_changes']} 处"
            "（按 published_at 重走，不调用模型）"
        ),
        (
            f"同期别人怎么看: {stats['before_period']} 篇 → {stats['after_period']} 篇"
            "在 ±30 天里有别的作者的判断"
        ),
    ]
    shown = stats["changes"][:30]
    for item in shown:
        lines.append(
            f"  态度 {item['author']} {item['previous']} → {item['stance']} 文章 {item['article_id']}"
        )
    extra = len(stats["changes"]) - len(shown)
    if extra > 0:
        lines.append(f"  …还有 {extra} 处")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用归档里的真实 create_time 更正知识星球时间，并重排观点时间线")
    parser.add_argument("--source", required=True, help="知识星球来源 id，例如 fangwei-zsxq")
    parser.add_argument(
        "--input",
        type=Path,
        help="归档目录（含 md/ 和 raw/topics/）。不给就用库里正文开头的「- 时间：」",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args(argv)
    from researchlib.db import prepare
    from researchlib.ingest.parse import parse_archive
    from researchlib.sources import SourceRejected, enabled_sources, load_sources, require_source

    settings = Settings.from_env()
    try:
        source = require_source(load_sources(settings.sources_path), args.source, kind="zsxq")
    except SourceRejected as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2
    if args.input is not None and not args.input.exists():
        print(f"找不到归档：{args.input}", file=sys.stderr)
        return 1
    # 确认来源在启用列表里，重排态度变化时别的作者也算得上。
    if source.id not in {item.id for item in enabled_sources(load_sources(settings.sources_path))}:
        print(f"{source.id} 未启用", file=sys.stderr)
        return 2
    conn = prepare(settings)
    try:
        errors: list[str] = []
        if args.input is None:
            docs = docs_from_library(conn, source.id)
        else:
            with tempfile.TemporaryDirectory(prefix="research-retime-") as tmp:
                docs, errors, _files, _images = parse_archive(args.input, Path(tmp), id_mode="zsxq")
        for item in errors:
            print(item, file=sys.stderr)
        stats = refresh_clocks(
            conn, source, docs, settings, dry_run=args.dry_run, same_row=args.input is None
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(render_stats(stats))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

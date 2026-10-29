"""从 AIHOT 的 articles 表只读导入。未启用的来源整行拒绝，不写入。"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime

import psycopg
import yaml
from psycopg import sql
from psycopg.rows import dict_row

from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import Source, load_sources
from researchlib.textutil import derive_title, normalize_newlines, parse_datetime

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ImportMapError(Exception):
    pass


def assert_select_only(query: str) -> None:
    head = query.lstrip().split(None, 1)[0].lower()
    if head != "select":
        raise RuntimeError("AIHOT 连接只能执行 SELECT")


def load_import_map(path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    table = str(data.get("table") or "articles")
    if not _IDENT.match(table):
        raise ImportMapError(f"非法表名：{table}")
    columns = data.get("columns") or {}
    source_map = {str(key): str(value) for key, value in (data.get("source_map") or {}).items()}
    return {"table": table, "columns": columns, "source_map": source_map}


def resolve_columns(present: set[str], spec: dict) -> dict[str, str]:
    resolved: dict[str, str] = {}
    required = ("external_id", "title", "body", "source")
    for role, options in spec.items():
        if not isinstance(options, list):
            continue
        for name in options:
            if name in present and _IDENT.match(str(name)):
                resolved[role] = str(name)
                break
    missing = [role for role in required if role not in resolved]
    if missing:
        raise ImportMapError(
            f"AIHOT articles 表缺少 {', '.join(missing)}。现有列：{', '.join(sorted(present)) or '（无）'}"
        )
    return resolved


def map_source(value, sources: list[Source], explicit: dict[str, str]) -> Source | None:
    key = str(value or "").strip()
    target = explicit.get(key, key)
    by_id = {source.id: source for source in sources}
    source = by_id.get(target)
    if source is None or not source.enabled:
        return None
    return source


def _looks_like_html(text: str) -> bool:
    sample = text.lstrip()[:200].lower()
    return sample.startswith("<!doctype") or sample.startswith("<html") or "<p" in sample or "<div" in sample


def row_to_doc(row: dict, columns: dict[str, str], source: Source) -> ParsedDoc | None:
    body = row.get(columns["body"])
    if body is None or not str(body).strip():
        return None
    body_text = str(body)
    title_value = row.get(columns["title"])
    title = str(title_value).strip() if title_value else ""
    if not title:
        title = derive_title({}, body_text, "aihot.md")
    published = None
    if "published_at" in columns:
        published = parse_datetime(row.get(columns["published_at"]))
    external = str(row.get(columns["external_id"]))
    fmt = "html" if _looks_like_html(body_text) else "markdown"
    meta = {"format": fmt, "origin": "aihot", "parent": ""}
    if "url" in columns and row.get(columns["url"]):
        meta["url"] = str(row[columns["url"]])
    return ParsedDoc(
        external_id=f"aihot:{external}"[:500],
        title=title[:300],
        published_at=published if isinstance(published, datetime) else published,
        body_md=normalize_newlines(body_text),
        raw_meta=meta,
        image_refs=[],
    )


def _open_source(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url, row_factory=dict_row)
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    return conn


_TIME_TYPES = ("timestamp with time zone", "timestamp without time zone", "date")


def source_values(sources: list[Source], explicit: dict[str, str]) -> list[str]:
    """AIHOT 那边哪些来源值会映射到本库已启用的来源。只读这些行，别的来源的正文不碰。"""
    enabled = {source.id for source in sources if source.enabled}
    values = {key for key, target in explicit.items() if target in enabled}
    values |= {value for value in enabled if value not in explicit}
    return sorted(values)


def import_aihot(dest, settings: Settings, *, dry_run: bool = False, days: int | None = None) -> tuple:
    """days：只读发布时间在最近这么多天里的行（定时增量用）。不传就全读。"""
    spec = load_import_map(settings.aihot_map_path)
    if not settings.aihot_database_url:
        raise ImportMapError("没有 AIHOT_DATABASE_URL，不能导入")
    sources = load_sources(settings.sources_path)
    src = _open_source(settings.aihot_database_url)
    try:
        columns_query = """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """
        assert_select_only(columns_query)
        types = {
            row["column_name"]: row["data_type"]
            for row in src.execute(columns_query, (spec["table"],)).fetchall()
        }
        present = set(types)
        if not present:
            raise ImportMapError(f"找不到 public.{spec['table']}")
        columns = resolve_columns(present, spec["columns"])
        field_names = list(dict.fromkeys(columns.values()))
        conditions = [
            sql.SQL("{source}::text = ANY(%(values)s)").format(source=sql.Identifier(columns["source"]))
        ]
        params: dict = {"values": source_values(sources, spec["source_map"])}
        published = columns.get("published_at")
        if days and published and types.get(published) in _TIME_TYPES:
            conditions.append(
                sql.SQL("{published} >= now() - make_interval(days => %(days)s)").format(
                    published=sql.Identifier(published)
                )
            )
            params["days"] = int(days)
        query = sql.SQL("SELECT {fields} FROM {table} WHERE {where}").format(
            fields=sql.SQL(", ").join(sql.Identifier(name) for name in field_names),
            table=sql.Identifier(spec["table"]),
            where=sql.SQL(" AND ").join(conditions),
        )
        assert_select_only(query.as_string(src))
        # 白名单以外的行只数一下，计入「拒绝」，不读正文。
        others = sql.SQL("SELECT count(*) AS n FROM {table} WHERE {where}").format(
            table=sql.Identifier(spec["table"]),
            where=sql.SQL(" AND ").join([sql.SQL("NOT ({first})").format(first=conditions[0]), *conditions[1:]]),
        )
        assert_select_only(others.as_string(src))
        docs_by_source: dict[str, list[ParsedDoc]] = {}
        rejected = int(src.execute(others, params).fetchone()["n"])
        errors: list[str] = []
        with src.cursor(name="aihot_read") as cur:
            cur.itersize = 200
            cur.execute(query, params)
            for row in cur:
                source = map_source(row.get(columns["source"]), sources, spec["source_map"])
                if source is None:
                    rejected += 1
                    continue
                try:
                    doc = row_to_doc(row, columns, source)
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                if doc is None:
                    errors.append(f"空正文：{row.get(columns['external_id'])}")
                    continue
                docs_by_source.setdefault(source.id, []).append(doc)
    finally:
        src.close()
    if dry_run:
        from researchlib.ingest.model import IngestReport

        total = sum(len(items) for items in docs_by_source.values())
        report = IngestReport(
            source_id="aihot",
            source_name="AIHOT 只读预览",
            archive_files=total + rejected,
            rejected=rejected,
            errors=errors,
        )
        report.unchanged = total
        return report
    reports = []
    by_id = {source.id: source for source in sources}
    first = True
    for source_id, docs in docs_by_source.items():
        report = store_documents(
            dest,
            source=by_id[source_id],
            docs=docs,
            data_dir=settings.data_dir,
            archive_files=len(docs),
            images_in_archive=0,
            errors=errors if first else [],
            rejected=rejected if first else 0,
            origin="aihot",
        )
        first = False
        reports.append(report)
    if not reports:
        from researchlib.ingest.model import IngestReport

        return IngestReport(
            source_id="aihot",
            source_name="AIHOT",
            archive_files=rejected,
            rejected=rejected,
            errors=errors,
        )
    if len(reports) == 1:
        return reports[0]
    merged = reports[0]
    for extra in reports[1:]:
        merged.archive_files += extra.archive_files
        merged.inserted += extra.inserted
        merged.updated += extra.updated
        merged.unchanged += extra.unchanged
        merged.duplicates += extra.duplicates
        merged.db_rows += extra.db_rows
        merged.errors.extend(extra.errors)
        merged.follow_ids.extend(extra.follow_ids)
    merged.source_id = "aihot"
    merged.source_name = "AIHOT（多个来源）"
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 AIHOT 的 Postgres 只读导入白名单内的文章")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入本库")
    parser.add_argument("--days", type=int, default=None, help="只读最近这么多天发布的（定时增量用）")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        report = import_aihot(conn, settings, dry_run=args.dry_run, days=args.days)
        if not args.dry_run:
            from researchlib.ingest.follow import kick_ingest_followups

            kick_ingest_followups(conn, settings, list(getattr(report, "follow_ids", ()) or []))
    except ImportMapError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(report.render())
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())

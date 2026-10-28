"""各导入命令共用的参数和白名单闸门。"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.ingest.parse import parse_archive
from researchlib.ingest.store import store_documents
from researchlib.sources import SourceRejected, load_sources, require_source
from researchlib.zsxq_comments import source_owners


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--source", required=True, help="白名单里的来源 id")
    parser.add_argument("--input", required=True, type=Path, help="归档目录或 zip")
    parser.add_argument(
        "--origin",
        choices=("archive", "crawl"),
        default="archive",
        help="archive：手动导入的归档；crawl：定时抓取脚本导入（首页断更提示只看定时导入的来源）",
    )
    return parser


def run_markdown_ingest(
    kind: str, description: str, argv: list[str] | None, *, id_mode: str, after_store=None
) -> int:
    parser = build_parser(description)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    try:
        source = require_source(load_sources(settings.sources_path), args.source, kind=kind)
    except SourceRejected as exc:
        print(f"拒绝导入：{exc}", file=sys.stderr)
        return 2
    if not args.input.exists():
        print(f"找不到归档：{args.input}", file=sys.stderr)
        return 1
    conn = prepare(settings)
    try:
        with tempfile.TemporaryDirectory(prefix="research-ingest-") as tmp:
            docs, errors, archive_files, image_count = parse_archive(
                args.input, Path(tmp), id_mode=id_mode, owners=source_owners(source)
            )
            report = store_documents(
                conn,
                source=source,
                docs=docs,
                data_dir=settings.data_dir,
                archive_files=archive_files,
                images_in_archive=image_count,
                errors=errors,
                origin=args.origin,
            )
            print(report.render())
            if after_store is not None and not report.errors:
                after_store(conn, source)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 1 if report.errors else 0

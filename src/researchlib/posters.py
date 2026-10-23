"""知识星球发帖人。

话题 JSON 里 talk / answer 的 owner 是发帖人；只有经来源、group 与真实账户
绑定的星主正文或回复才入选。显示名不提供身份证据；未知记录与旧缓存保留，
由统一资格条件隔离，不删除原始数据。

    research-backfill-posters --source fangwei-zsxq --input /data/inbox/fangwei-zsxq
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.author_voice import assign_poster_role, is_guest


def backfill_posters(conn, source, docs: list) -> dict:
    """用归档里的话题 JSON 回填已入库文章的发帖人。返回计数。"""
    updated = guests = cleared = missing = 0
    for doc in docs:
        incoming = assign_poster_role(
            dict(doc.raw_meta or {}), source.author, source_id=source.id, source_kind=source.kind
        )
        name = str(incoming.get("poster_name") or "").strip()
        poster_id = str(incoming.get("poster_id") or "").strip()
        if not name and not poster_id:
            continue
        topic_id = str(incoming.get("topic_id") or doc.external_id or "")
        rows = conn.execute(
            """
            SELECT id, raw_meta
            FROM articles
            WHERE source_id = %s
              AND (external_id = %s OR raw_meta->>'topic_id' = %s)
            """,
            (source.id, doc.external_id, topic_id),
        ).fetchall()
        if not rows:
            missing += 1
            continue
        for row in rows:
            meta = dict(row["raw_meta"] or {})
            meta["poster_name"] = name
            meta["poster_id"] = poster_id
            for key in ("zsxq_identity", "group_id", "source_kind"):
                if key in incoming:
                    meta[key] = incoming[key]
            assign_poster_role(meta, source.author, source_id=source.id, source_kind=source.kind)
            conn.execute(
                "UPDATE articles SET raw_meta = %s WHERE id = %s",
                (Jsonb(meta), row["id"]),
            )
            updated += 1
            if is_guest(meta):
                guests += 1
                # Existing raw records and cached model output remain reversible;
                # eligibility guards remove them from every reader/worker.
    conn.commit()
    return {"updated": updated, "guests": guests, "views_cleared": cleared, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按知识星球话题 JSON 回填发帖人，网友帖从星主观点里拿掉")
    parser.add_argument("--source", required=True, help="来源 id，例如 fangwei-zsxq")
    parser.add_argument("--input", required=True, help="归档目录（含 md/ 与 raw/）")
    args = parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.ingest.parse import parse_archive
    from researchlib.sources import load_sources, require_source

    settings = Settings.from_env()
    source = require_source(load_sources(settings.sources_path), args.source, kind="zsxq")
    archive = Path(args.input)
    docs, errors, _files, _images = parse_archive(
        archive, archive / ".poster-work", id_mode="zsxq", source_id=source.id
    )
    for error in errors:
        print(error, file=sys.stderr)
    conn = prepare(settings)
    try:
        stats = backfill_posters(conn, source, docs)
    finally:
        conn.close()
    print(
        f"回填 {stats['updated']} 篇，其中网友帖 {stats['guests']} 篇，"
        f"从观点里拿掉 {stats['views_cleared']} 条，归档里对不上库的 {stats['missing']} 篇"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

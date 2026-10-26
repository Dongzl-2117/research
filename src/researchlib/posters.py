"""知识星球发帖人。

话题 JSON 里 talk / answer 的 owner 是发帖人。名字对得上来源作者（星主）才算原创；
对不上的是网友帖：正文留在库里，观点删掉，不进星主的作者统计。

    research-backfill-posters --source fangwei-zsxq --input /data/inbox/fangwei-zsxq

来源配置里的作者或 poster_names 改了，应用启动时按新名单重算已入库帖子的标记（refresh_poster_roles），
不用重新导入。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.author_voice import POSTER_ROLE, assign_poster_role, is_guest

logger = logging.getLogger("researchlib.posters")
# app_state 里记下每个星球来源上次按什么名单算的标记：poster_names:<来源>。
STATE_PREFIX = "poster_names:"


def refresh_poster_roles(conn, sources) -> dict[str, int]:
    """星球来源的作者或 poster_names 和上次不一样，就按新名单重算库里帖子的发帖人标记。

    返回 {来源: 改了几篇}，名单没变的来源不在里面。改成网友帖的删掉观点；改回星主原创的，
    观点由夜间 research-views 补抽。调用方负责提交。
    """
    changed: dict[str, int] = {}
    for source in sources:
        if source.kind != "zsxq":
            continue
        key = STATE_PREFIX + source.id
        names = json.dumps([source.author, *source.poster_names], ensure_ascii=False)
        state = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
        if state is not None and state["value"] == names:
            continue
        flipped = 0
        rows = conn.execute(
            "SELECT id, raw_meta FROM articles WHERE source_id = %s AND coalesce(raw_meta->>'poster_name', '') <> ''",
            (source.id,),
        ).fetchall()
        for row in rows:
            meta = dict(row["raw_meta"] or {})
            before = meta.get(POSTER_ROLE)
            assign_poster_role(meta, source.author, source.poster_names)
            if meta.get(POSTER_ROLE) == before:
                continue
            conn.execute("UPDATE articles SET raw_meta = %s WHERE id = %s", (Jsonb(meta), row["id"]))
            if is_guest(meta):
                conn.execute("DELETE FROM company_views WHERE article_id = %s", (row["id"],))
            flipped += 1
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at) VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, names),
        )
        if flipped:
            logger.warning("%s 的发帖人名单变了，重算了 %s 篇的原创标记", source.id, flipped)
        changed[source.id] = flipped
    return changed


def backfill_posters(conn, source, docs: list) -> dict:
    """用归档里的话题 JSON 回填已入库文章的发帖人。返回计数。"""
    updated = guests = cleared = missing = 0
    for doc in docs:
        incoming = assign_poster_role(dict(doc.raw_meta or {}), source.author, source.poster_names)
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
            assign_poster_role(meta, source.author, source.poster_names)
            conn.execute(
                "UPDATE articles SET raw_meta = %s WHERE id = %s",
                (Jsonb(meta), row["id"]),
            )
            updated += 1
            if is_guest(meta):
                guests += 1
                cur = conn.execute("DELETE FROM company_views WHERE article_id = %s", (row["id"],))
                cleared += cur.rowcount
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
    docs, errors, _files, _images = parse_archive(archive, archive / ".poster-work", id_mode="zsxq")
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

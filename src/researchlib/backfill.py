"""给已入库、但 published_at 为空的文章补日期。

只填空值。日期来自正文 frontmatter、raw_meta 或文件名，和导入用的是同一套规则。
"""

from __future__ import annotations

import sys

from psycopg.types.json import Jsonb

from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.textutil import content_hash, resolve_published_at, split_frontmatter


def backfill_published_at(conn) -> dict:
    rows = conn.execute(
        """
        SELECT id, title, body_md, raw_meta, external_id
        FROM articles
        WHERE published_at IS NULL
        """
    ).fetchall()
    updated = 0
    for row in rows:
        meta = dict(row["raw_meta"] or {})
        frontmatter, _body = split_frontmatter(row["body_md"] or "")
        filename = str(meta.get("filename") or "")
        when, source = resolve_published_at(
            frontmatter=frontmatter,
            hints=meta,
            filename=filename,
            text=row["body_md"] or "",
            extra_filenames=(str(row["external_id"] or ""),),
        )
        if when is None:
            continue
        meta["date_source"] = source
        digest = content_hash(row["title"], row["body_md"], when)
        conn.execute(
            """
            UPDATE articles
            SET published_at = %s,
                content_hash = %s,
                raw_meta = %s,
                updated_at = now()
            WHERE id = %s AND published_at IS NULL
            """,
            (when, digest, Jsonb(meta), row["id"]),
        )
        updated += 1
    remaining = conn.execute("SELECT count(*) AS n FROM articles WHERE published_at IS NULL").fetchone()
    return {"scanned": len(rows), "updated": updated, "remaining": int(remaining["n"])}


def main(argv: list[str] | None = None) -> int:
    if argv:
        raise SystemExit("research-backfill-dates 不接受参数")
    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        stats = backfill_published_at(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"扫描: {stats['scanned']}")
    print(f"写上日期: {stats['updated']}")
    print(f"仍无日期: {stats['remaining']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

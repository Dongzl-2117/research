#!/usr/bin/env python3
"""Backfill content_hash after timezone-stable stamping.

``content_hash`` now always stamps ``published_at`` in Asia/Shanghai. Rows that
were hashed from a UTC ``isoformat()`` after a DB round-trip no longer match.
This script recomputes every article hash, realigns digests / views /
translations / fingerprints that still point at the old value, and also
reattaches complete translations whose hash is only a timezone or repaired-date
variant of the current title/body.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from researchlib.config import Settings  # noqa: E402
from researchlib.db import prepare  # noqa: E402
from researchlib.retime import _realign  # noqa: E402
from researchlib.textutil import SHANGHAI, content_hash, normalize_newlines  # noqa: E402


def _stamp_hash(title: str, body_md: str, stamp: str) -> str:
    payload = f"{title}\n{stamp}\n{normalize_newlines(body_md)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _variant_hashes(title: str, body_md: str, published_at, raw_meta: dict | None) -> set[str]:
    out: set[str] = set()
    if published_at is not None:
        out.add(_stamp_hash(title, body_md, published_at.astimezone(UTC).isoformat()))
        out.add(_stamp_hash(title, body_md, published_at.astimezone(SHANGHAI).isoformat()))
        out.add(_stamp_hash(title, body_md, published_at.replace(tzinfo=None).isoformat()))
    meta = raw_meta or {}
    repaired = meta.get("repaired_from")
    if isinstance(repaired, str) and repaired.strip():
        out.add(_stamp_hash(title, body_md, repaired.strip()))
        # common Shanghai wall form of a UTC repaired_from
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(repaired.strip().replace("Z", "+00:00"))
            out.add(_stamp_hash(title, body_md, parsed.astimezone(SHANGHAI).isoformat()))
            out.add(_stamp_hash(title, body_md, parsed.astimezone(UTC).isoformat()))
        except ValueError:
            pass
    return {item for item in out if item}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default is dry-run)")
    parser.add_argument("--source", default="", help="optional source_id filter")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    conn = prepare(settings)
    article_updates = 0
    realign_counts = {"views": 0, "digests": 0, "translations": 0, "fingerprints": 0}
    orphan_translations = 0
    try:
        params: list[object] = []
        where = "WHERE duplicate_of IS NULL"
        if args.source:
            where += " AND source_id = %s"
            params.append(args.source)
        rows = conn.execute(
            f"""
            SELECT id, title, body_md, published_at, content_hash, raw_meta
            FROM articles
            {where}
            ORDER BY id
            """,
            params,
        ).fetchall()
        for row in rows:
            target = content_hash(row["title"], row["body_md"], row["published_at"])
            old = row["content_hash"] or ""
            if old != target:
                article_updates += 1
                if args.apply:
                    counts = _realign(conn, row["id"], {old}, target)
                    for key, value in counts.items():
                        realign_counts[key] = realign_counts.get(key, 0) + value
                    conn.execute(
                        "UPDATE articles SET content_hash = %s WHERE id = %s AND content_hash = %s",
                        (target, row["id"], old),
                    )

        # Complete translations that still disagree: only realign when the stored
        # hash is a known timezone / repaired-date variant of the current body.
        translations = conn.execute(
            """
            SELECT t.article_id, t.content_hash AS t_hash, a.content_hash AS a_hash,
                   a.title, a.body_md, a.published_at, a.raw_meta
            FROM article_translations t
            JOIN articles a ON a.id = t.article_id
            WHERE t.complete
              AND t.content_hash IS DISTINCT FROM a.content_hash
            """
            + (" AND a.source_id = %s" if args.source else ""),
            [args.source] if args.source else [],
        ).fetchall()
        for row in translations:
            target = content_hash(row["title"], row["body_md"], row["published_at"])
            variants = _variant_hashes(row["title"], row["body_md"], row["published_at"], row["raw_meta"])
            # Only reattach when the stored translation hash is the same body under a
            # timezone / repaired-date stamp — never invent a match for unrelated text.
            if row["t_hash"] != target and row["t_hash"] not in variants:
                continue
            orphan_translations += 1
            if args.apply:
                conn.execute(
                    """
                    UPDATE article_translations
                    SET content_hash = %s, updated_at = now()
                    WHERE article_id = %s AND content_hash = %s
                    """,
                    (target, row["article_id"], row["t_hash"]),
                )

        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"{mode}: article_hash_updates={article_updates} "
        f"orphan_translations={orphan_translations} realign={realign_counts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Repair Stratechery articles whose published_at was stamped with the crawl day.

Root cause (2026-10-22 full archive): ~76 inbox markdown files got
`published`/`date: 2026-10-21` when the crawler could not read a real publish
date. Ingest trusted frontmatter → Asia/Shanghai midnight →
`2026-10-20 16:00:00+00`, so the author page piled them on 「10月21日」.

This script:
1. Selects rows with that exact wrong timestamp (or --all-crawl-day).
2. Looks up each slug on https://stratechery.com/wp-json/wp/v2/posts.
3. Rewrites inbox frontmatter `published`/`date` to the WP local calendar day
   (same convention as the rest of the archive).
4. Updates `articles.published_at` / `content_hash` / `raw_meta` to match
   `resolve_published_at` (Shanghai midnight of that day).

Safe to re-run. Does not touch translations, embeddings, or other sources.
Does not stop long-running jobs.

Usage (from repo root, with DB reachable):

  DATABASE_URL=... python scripts/repair_stratechery_dates.py --dry-run
  DATABASE_URL=... python scripts/repair_stratechery_dates.py

Or inside the app container (mount / workspace already has the repo):

  docker compose exec -T app python /app/scripts/repair_stratechery_dates.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# Allow `python scripts/...` without installing the package when run on host.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from psycopg.types.json import Jsonb  # noqa: E402

from researchlib.config import Settings  # noqa: E402
from researchlib.db import prepare  # noqa: E402
from researchlib.textutil import content_hash, parse_datetime, split_frontmatter  # noqa: E402

WRONG_TS = datetime.fromisoformat("2026-10-20T16:00:00+00:00")
WP_ENDPOINT = "https://stratechery.com/wp-json/wp/v2/posts"
_FRONT_DATE = re.compile(
    r'^((?:published|date)\s*:\s*)(["\']?)([^"\'\n]+)(["\']?\s*)$',
    re.M,
)
_UA = "research-repair-stratechery-dates/1.0 (+https://investment.zldong.com)"


def _inbox_root(settings: Settings) -> Path:
    # Prefer the live inbox next to compose; fall back to Settings if present.
    candidates = [
        _ROOT / "data" / "inbox" / "stratechery",
        Path("/data/inbox/stratechery"),
        Path(getattr(settings, "data_dir", _ROOT / "data")) / "inbox" / "stratechery",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def _slug(external_id: str) -> str:
    return external_id.rsplit("/", 1)[-1]


def _wp_lookup(slug: str, sleep_s: float) -> dict | None:
    query = urllib.parse.urlencode(
        {"slug": slug, "_fields": "id,date,date_gmt,slug,link,modified,modified_gmt"}
    )
    req = urllib.request.Request(
        f"{WP_ENDPOINT}?{query}",
        headers={"User-Agent": _UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            time.sleep(max(sleep_s, 5.0))
            return _wp_lookup(slug, sleep_s)
        raise
    time.sleep(sleep_s)
    if not payload:
        return None
    return payload[0]


def _calendar_day(wp_date: str) -> str:
    # WP `date` is site-local wall time; archive frontmatter stores its calendar day.
    return wp_date[:10]


def _rewrite_frontmatter(text: str, day: str) -> str:
    frontmatter, body = split_frontmatter(text)
    if not frontmatter and not text.startswith("---"):
        # No frontmatter — prepend a minimal block.
        return (
            "---\n"
            f'published: "{day}"\n'
            f'date: "{day}"\n'
            "---\n\n"
            f"{text.lstrip()}"
        )

    def repl(match: re.Match[str]) -> str:
        key, q1, _old, q2 = match.groups()
        # Keep original quoting style when present.
        if q1:
            return f"{key}{q1}{day}{q2}"
        return f'{key}"{day}"'

    updated, n = _FRONT_DATE.subn(repl, text, count=0)
    if n == 0:
        # Insert published/date after opening ---
        updated = re.sub(
            r"\A---\s*\n",
            f'---\npublished: "{day}"\ndate: "{day}"\n',
            text,
            count=1,
        )
    # Ensure both keys exist.
    if not re.search(r"^published\s*:", updated, re.M):
        updated = re.sub(
            r"\A---\s*\n",
            f'---\npublished: "{day}"\n',
            updated,
            count=1,
        )
    if not re.search(r"^date\s*:", updated, re.M):
        updated = re.sub(
            r"^(published\s*:\s*.+)$",
            rf'\1\ndate: "{day}"',
            updated,
            count=1,
            flags=re.M,
        )
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.8,
        help="seconds between WP API calls (default 0.8)",
    )
    parser.add_argument(
        "--mapping-out",
        type=Path,
        default=_ROOT / "tmp" / "stratechery-date-repair-mapping.json",
    )
    parser.add_argument(
        "--skip-inbox",
        action="store_true",
        help="only update DB, leave inbox markdown alone",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    inbox = _inbox_root(settings)
    conn = prepare(settings)
    rows = conn.execute(
        """
        SELECT id, external_id, title, body_md, raw_meta, published_at
        FROM articles
        WHERE source_id = 'stratechery'
          AND published_at = %s
        ORDER BY external_id
        """,
        (WRONG_TS,),
    ).fetchall()

    print(f"bad rows: {len(rows)}")
    print(f"inbox: {inbox} exists={inbox.is_dir()}")

    mapping: list[dict] = []
    updated = 0
    missing_wp = 0
    missing_file = 0

    try:
        for row in rows:
            external_id = row["external_id"]
            slug = _slug(external_id)
            wp = _wp_lookup(slug, args.sleep)
            if not wp:
                print(f"MISS wp-json slug={slug} id={row['id']} ext={external_id}")
                missing_wp += 1
                mapping.append(
                    {
                        "id": row["id"],
                        "external_id": external_id,
                        "slug": slug,
                        "status": "wp_missing",
                    }
                )
                continue

            day = _calendar_day(wp["date"])
            when = parse_datetime(day)
            assert when is not None

            md_path = inbox / f"{external_id}.md"
            inbox_ok = md_path.is_file()
            if not inbox_ok:
                missing_file += 1

            entry = {
                "id": row["id"],
                "external_id": external_id,
                "slug": slug,
                "old_published_at": row["published_at"].isoformat(),
                "wp_date": wp["date"],
                "wp_date_gmt": wp.get("date_gmt"),
                "new_day": day,
                "new_published_at": when.isoformat(),
                "inbox_path": str(md_path) if inbox_ok else None,
                "status": "ok",
            }
            mapping.append(entry)
            print(
                f"FIX id={row['id']} {external_id} -> {day} "
                f"(wp {wp['date']}) inbox={'yes' if inbox_ok else 'NO'}"
            )

            if args.dry_run:
                continue

            if inbox_ok and not args.skip_inbox:
                original = md_path.read_text(encoding="utf-8")
                rewritten = _rewrite_frontmatter(original, day)
                if rewritten != original:
                    md_path.write_text(rewritten, encoding="utf-8")

            meta = dict(row["raw_meta"] or {})
            meta["date_source"] = "wp_json_repair"
            meta["time_known"] = False
            meta["wp_date"] = wp["date"]
            meta["wp_date_gmt"] = wp.get("date_gmt")
            meta["repaired_from"] = "2026-10-20T16:00:00+00:00"
            digest = content_hash(row["title"], row["body_md"], when)
            conn.execute(
                """
                UPDATE articles
                SET published_at = %s,
                    content_hash = %s,
                    raw_meta = %s,
                    updated_at = now()
                WHERE id = %s
                  AND source_id = 'stratechery'
                  AND published_at = %s
                """,
                (when, digest, Jsonb(meta), row["id"], WRONG_TS),
            )
            updated += 1

        if not args.dry_run:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    args.mapping_out.parent.mkdir(parents=True, exist_ok=True)
    args.mapping_out.write_text(
        json.dumps(
            {
                "wrong_ts": WRONG_TS.isoformat(),
                "dry_run": args.dry_run,
                "count": len(mapping),
                "updated": updated,
                "missing_wp": missing_wp,
                "missing_file": missing_file,
                "items": mapping,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"done dry_run={args.dry_run} updated={updated} "
        f"missing_wp={missing_wp} missing_file={missing_file} "
        f"mapping={args.mapping_out}"
    )
    return 0 if missing_wp == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

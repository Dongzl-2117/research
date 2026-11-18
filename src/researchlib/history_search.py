"""按历史归档时点查找字面证据，输出一个有界分页的 JSON；不访问检索索引和模型。"""

from __future__ import annotations

import argparse
import json
import re
from urllib.parse import urlencode

import psycopg
from psycopg.rows import dict_row

from researchlib.article_history import parse_as_of, read_version
from researchlib.config import Settings
from researchlib.sources import enabled_sources, load_sources

MAX_SCAN = 250
MAX_ID = 2**63 - 1


def _first_hit(pattern, version):
    fields = [("title", version["title"], None), ("body_markdown", version["body_markdown"], None)]
    fields.extend(("author_reply", r["text"], r["published_at"]) for r in version["author_replies"])
    for field, text, reply_at in fields:
        match = pattern.search(text)
        if match:
            return {
                "field": field,
                "match": match.group(),
                "author_reply_published_at": reply_at,
                "excerpt": text[max(0, match.start() - 120) : match.end() + 120],
            }
    return None


def search_page(conn, sources, base, *, query, as_of, after_id=0, through_id=None, scan_limit=100):
    """仅在已确认版本里判断是否命中。当前可见性只用于收回资料；从未归档的文章无法重建。"""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
        raise ValueError("关键词须为 1 至 120 个字符")
    if not 1 <= scan_limit <= MAX_SCAN:
        raise ValueError(f"每页扫描篇数须为 1 至 {MAX_SCAN}")
    if not 0 <= after_id <= MAX_ID or (through_id is not None and not after_id <= through_id <= MAX_ID):
        raise ValueError("游标或上界不是有效的文章编号")
    cutoff = parse_as_of(as_of)
    if cutoff is None or cutoff > conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]:
        raise ValueError("必须提供一个已经过去的归档时点")
    pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
    if through_id is None:
        through_id = conn.execute("SELECT coalesce(max(id), 0) AS n FROM articles").fetchone()["n"]
    # 不以当前发表时间、标题、正文、公司标签或向量选择候选；只枚举自增编号。
    rows = conn.execute(
        "SELECT id FROM articles WHERE id > %s AND id <= %s ORDER BY id LIMIT %s",
        (after_id, through_id, scan_limit + 1),
    ).fetchall()
    enabled_ids = {r["id"] for r in conn.execute("SELECT id FROM sources WHERE enabled")}
    by_id = {s.id: s for s in enabled_sources(sources) if s.id in enabled_ids}
    coverage = dict.fromkeys(("verified", "unverified", "not_yet_archived", "unavailable"), 0)
    matches = []
    for row in rows[:scan_limit]:
        version = read_version(conn, row["id"], cutoff, by_id)
        status = version["status"] if version else "unavailable"
        coverage[status] += 1
        if status == "verified" and (hit := _first_hit(pattern, version)):
            matches.append(
                {
                    "article_id": row["id"],
                    "title": version["title"],
                    "source_id": version["source"]["id"],
                    **{k: version[k] for k in ("published_at", "version_written_at", "confirmed_at")},
                    "url": f"{base.rstrip('/')}/articles/{row['id']}/history?"
                    + urlencode({"as_of": cutoff.isoformat()}),
                    **hit,
                }
            )
    return {
        "as_of": cutoff.isoformat(),
        "query": query.strip(),
        "through_id": through_id,
        "after_id": after_id,
        "scanned": min(len(rows), scan_limit),
        "coverage": coverage,
        "matches": matches,
        "next_after_id": rows[scan_limit - 1]["id"] if len(rows) > scan_limit else None,
        "status": "matches"
        if matches
        else ("no_match_in_verified" if coverage["verified"] else "insufficient_evidence"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="连续出现的关键词或短句，英文字母忽略大小写")
    parser.add_argument("--as-of", required=True, help="历史时点；未给时区时按上海时间")
    parser.add_argument("--scan-limit", type=int, default=100, help=f"每页扫描篇数（1 至 {MAX_SCAN}）")
    parser.add_argument("--after-id", type=int, default=0, help="上一页的 next_after_id")
    parser.add_argument("--through-id", type=int, help="第一页的 through_id；翻页时沿用")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    try:
        # 同一页的可见性、候选和版本都取自同一个只读数据库快照。
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            result = search_page(conn, load_sources(settings.sources_path), settings.public_base_url, **vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
        raise SystemExit("数据库缺少历史归档结构。请使用已应用迁移 041、042 的研究库。") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

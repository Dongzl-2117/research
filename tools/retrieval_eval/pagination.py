"""在显式指定的隔离合成库复现分页缺口并测耗时，不访问默认库或外部模型。

    python tools/retrieval_eval/pagination.py \
      --url postgresql://research:research@127.0.0.1:55432/research_eval_pagination --check

只允许 research_eval_pagination 开头的数据库；会清空其中的文章。修改前后用同一命令、同一规模比较。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from researchlib import retrieval
from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.queries import Scope

ROOT = Path(__file__).resolve().parents[2]
WORD = "paginationneedle"


def seed(conn, count, long_chunks, word):
    conn.execute("TRUNCATE articles RESTART IDENTITY CASCADE")
    conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)
        SELECT 'fangwei-mp', 'pagination:' || n, 'synthetic:' || n, '合成研究 ' || n,
               '2024-06-01 00:00:00+00'::timestamptz,
               %(word)s || ' 的长期供需变化能够解释收益。',
               %(word)s || ' 的长期供需变化能够解释收益。'
        FROM generate_series(1, %(count)s) n
        """, {"word": word, "count": count},
    )
    conn.execute("INSERT INTO article_chunks (article_id, ord, body) SELECT id, 0, body_text FROM articles")
    long_id = conn.execute(
        "INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)"
        " VALUES ('fangwei-mp', 'pagination:long', 'synthetic:long', '合成长研究',"
        " '2024-06-01 00:00:00+00', repeat(%s, %s), repeat(%s, %s)) RETURNING id",
        (word + '\n', long_chunks, word + '\n', long_chunks),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, body)"
        " SELECT %s, n, %s FROM generate_series(0, %s - 1) n", (long_id, word, long_chunks),
    )
    conn.execute("ANALYZE articles")
    conn.execute("ANALYZE article_chunks")
    conn.commit()
    return set(range(1, count + 2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--articles", type=int, default=6000)
    parser.add_argument("--long-chunks", type=int, default=2301)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--query", default=WORD, help="合成正文中的检索词；两个词可覆盖 AND 召回窗口")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--check", action="store_true", help="缺篇、重复、计数错误或换页顺序变化时失败")
    args = parser.parse_args()
    database = urlsplit(args.url).path.removeprefix("/")
    if not database.startswith("research_eval_pagination"):
        parser.error("只允许显式指定 research_eval_pagination 开头的隔离库，会清空其中的合成文章")
    if args.articles < 1 or not 1 <= args.long_chunks < 9000 or not 1 <= args.page_size <= 100:
        parser.error("articles >= 1，long-chunks 在 1～8999，page-size 在 1～100")
    settings = Settings.from_env({
        "DATABASE_URL": args.url, "DATA_DIR": str(ROOT / "data" / "eval-pagination"),
        "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
    })
    # Settings 未接入进程环境中的向量配置；这里再显式替换，保证基准只测本地 SQL 和打分。
    retrieval.semantic_candidates = lambda _c, _q, _s, rows: (rows, {})
    with prepare(settings) as conn:
        expected = seed(conn, args.articles, args.long_chunks, args.query)
        sources = enabled_sources(load_sources(settings.sources_path))
        scope = Scope(source_ids=[s.id for s in sources])
        retrieval.reset_stats_cache()
        fetched = []
        original = retrieval._rows_for_keys

        def record(connection, keys):
            fetched.append(len(keys))
            return original(connection, keys)

        retrieval._rows_for_keys = record
        latencies = []
        payloads = []

        def run(offset, limit=args.page_size):
            before = len(fetched)
            start = time.perf_counter()
            found = retrieval.search_articles(conn, args.query, scope, sources, limit=limit, offset=offset)
            latencies.append((time.perf_counter() - start) * 1000)
            payloads.append(sum(fetched[before:]))
            return found

        first = run(0)
        ordered = [h.article_id for h in first.hits]
        counts_ok = first.total == first.available == len(expected) and first.partial == 0
        for offset in range(args.page_size, first.available, args.page_size):
            page = run(offset)
            ordered.extend(h.article_id for h in page.hits)
            counts_ok &= page.total == first.total and page.available == first.available
            if offset % (args.page_size * 20) == 0:
                print(f"已翻 {len(ordered)} 篇", file=sys.stderr, flush=True)
        stable = True
        for offset in (0, 1993, max(0, len(ordered) - 7)):
            stable &= [h.article_id for h in run(offset, 17).hits] == ordered[offset:offset + 17]
        empty = not run(len(expected)).hits
        result = {
            "module": str(Path(retrieval.__file__).resolve()),
            "query": args.query,
            "articles": len(expected), "chunks": args.articles + args.long_chunks,
            "reported_total": first.total, "reported_available": first.available,
            "reachable": len(set(ordered)), "duplicates": len(ordered) - len(set(ordered)),
            "missing": len(expected - set(ordered)), "unexpected": len(set(ordered) - expected),
            "stable": stable, "counts_ok": counts_ok, "empty_after_end": empty,
            "first_ms": round(latencies[0], 2), "median_ms": round(statistics.median(latencies), 2),
            "max_ms": round(max(latencies), 2), "calls": len(latencies),
            "max_text_rows_per_call": max(payloads), "max_text_rows_per_fetch": max(fetched, default=0),
        }
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        print(output)
        if args.json:
            args.json.write_text(output, encoding="utf-8")
        if args.check and not (
            counts_ok and stable and empty and result["missing"] == result["duplicates"] == result["unexpected"] == 0
        ):
            raise SystemExit("分页完整性检查失败")


if __name__ == "__main__":
    main()

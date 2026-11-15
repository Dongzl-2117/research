"""只读：重复出现的图按两种判法各算一遍，列出结论不一样的（审核 C7）。不写库、不调模型。

现在的判法（``boilerplate._occasional``）看一张图第一次到最后一次出现之间，这一来源的文章有几成带着它，
低于 ``IMAGE_COVERAGE`` 算作者隔一阵才重用的示意图，不删。毛病：一张二维码在 33 篇里出现 10 篇（0.30），
半年后作者又贴一次，中间来源又发了 30 篇，11/64 = 0.17，它就从模板图翻成示意图，以前那 10 篇的推销尾巴
整段回来，清洗结果变了，对照回执也跟着过期。

候选判法：只看出现最密的那一段。取这张图至少一半的出现（不少于 ``IMAGE_MIN_DF`` 次）连在一起的窗口，
各窗口里带着它的比例取最大。以后隔很久再用一次，最密的那一段还在，结论不翻。反过来的风险是：作者在几周里
连用几次的示意图（Stratechery 的 Smiling Curve 这类）可能被判成模板图、从页面上删掉，正是 #325 刚修好的。
线上会翻哪些图要在正式库上算，所以先只出这份对照，结论不一样的逐张看过再决定改不改。

    docker compose exec -T app research-boilerplate-images              # 所有来源
    docker compose exec -T app research-boilerplate-images --source stratechery
"""

from __future__ import annotations

import argparse
import math
from bisect import bisect_left, bisect_right
from datetime import datetime

from researchlib.boilerplate import IMAGE_BATCH, IMAGE_COVERAGE, IMAGE_MIN_DF, LIBRARY_KINDS
from researchlib.config import Settings
from researchlib.db import connect


def overall_coverage(times: list[datetime], published: list[datetime]) -> float:
    """现在的判法：首末两次出现之间，来源的文章里带着这张图的比例。"""
    span = bisect_right(published, times[-1]) - bisect_left(published, times[0])
    return len(times) / max(span, 1)


def densest_coverage(times: list[datetime], published: list[datetime]) -> float:
    """候选判法：至少一半的出现（不少于 IMAGE_MIN_DF）连在一起的窗口里，带着它的比例取最大。"""
    k = max(IMAGE_MIN_DF, math.ceil(len(times) / 2))
    if len(times) < k:
        return overall_coverage(times, published)
    best = 0.0
    for start in range(len(times) - k + 1):
        first, last = times[start], times[start + k - 1]
        span = bisect_right(published, last) - bisect_left(published, first)
        best = max(best, k / max(span, 1))
    return best


def compare(conn, source_id: str) -> list[dict]:
    """这一来源里两种判法结论不一样的重复图。"""
    published = sorted(
        row["published_at"] for row in conn.execute(
            "SELECT published_at FROM articles WHERE source_id = %s AND duplicate_of IS NULL"
            " AND published_at IS NOT NULL", (source_id,),
        ).fetchall()
    )
    rows = conn.execute(
        """
        SELECT content_hash,
               array_agg(id ORDER BY id) AS ids,
               array_agg(published_at ORDER BY published_at) FILTER (WHERE published_at IS NOT NULL) AS times
        FROM (
          SELECT DISTINCT i.content_hash, a.id, a.published_at
          FROM article_images i JOIN articles a ON a.id = i.article_id
          WHERE i.source_id = %s AND a.duplicate_of IS NULL
        ) appearances
        GROUP BY content_hash
        HAVING count(*) >= %s
        """,
        (source_id, IMAGE_MIN_DF),
    ).fetchall()
    batches: dict[tuple, int] = {}
    for row in rows:
        batches[tuple(row["ids"])] = batches.get(tuple(row["ids"]), 0) + 1
    found = []
    for row in rows:
        if batches[tuple(row["ids"])] >= IMAGE_BATCH or not row["times"]:
            continue
        times = list(row["times"])
        now = overall_coverage(times, published)
        dense = densest_coverage(times, published)
        if (now < IMAGE_COVERAGE) != (dense < IMAGE_COVERAGE):
            found.append({
                "hash": row["content_hash"], "articles": len(row["ids"]),
                "now": now, "densest": dense, "sample": list(row["ids"][:5]),
                "first": times[0], "last": times[-1],
            })
    return sorted(found, key=lambda item: -item["articles"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读：重复图按现在和候选两种判法的结论不一样的（审核 C7）")
    parser.add_argument("--source", action="append", default=[], help="只看这个来源，可重复")
    args = parser.parse_args(argv)
    conn = connect(Settings.from_env())
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        sources = [row["id"] for row in conn.execute(
            "SELECT id, kind FROM sources ORDER BY id").fetchall() if row["kind"] not in LIBRARY_KINDS]
        if args.source:
            sources = [source for source in sources if source in set(args.source)]
        for source_id in sources:
            found = compare(conn, source_id)
            print(f"{source_id}：两种判法结论不一样的图 {len(found)} 张")
            for item in found:
                turn = ("示意图 → 模板图（会从页面删掉）" if item["now"] < IMAGE_COVERAGE
                        else "模板图 → 示意图（会留在页面上）")
                print(f"  {item['hash'][:12]} 出现在 {item['articles']} 篇，现在 {item['now']:.2f}、最密一段 "
                      f"{item['densest']:.2f}：{turn}；{item['first']:%Y-%m-%d} 到 {item['last']:%Y-%m-%d}，"
                      f"例 " + "、".join(f"#{article_id}" for article_id in item["sample"]))
    finally:
        conn.rollback()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

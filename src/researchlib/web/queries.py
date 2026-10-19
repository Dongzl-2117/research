"""页面用到的 SQL。只读已启用来源。"""

from __future__ import annotations


def _shanghai_day(column: str = "published_at") -> str:
    # 与 company_months 一样，按 Asia/Shanghai 的日历日切桶。
    return f"date_trunc('day', {column} AT TIME ZONE 'Asia/Shanghai')"

# 标题权重大于正文；作者名单独匹配，不要求出现在正文里。
_SEARCH_MATCH = """
(
  a.search_tsv @@ q
  OR to_tsvector('chinese', coalesce(s.author, '')) @@ q
  OR (char_length(%(raw)s) >= 2 AND position(%(raw)s in s.author) > 0)
)
"""
_SEARCH_RANK = """
(
  5 * ts_rank_cd(setweight(to_tsvector('chinese', coalesce(a.title, '')), 'A'), q)
  + ts_rank_cd(a.search_tsv, q)
  + CASE
      WHEN s.author = %(raw)s THEN 3
      WHEN char_length(%(raw)s) >= 2 AND position(%(raw)s in s.author) > 0 THEN 2
      WHEN to_tsvector('chinese', coalesce(s.author, '')) @@ q THEN 1.5
      ELSE 0
    END
)
"""


def feed_counts(conn, source_ids: list[str]) -> dict:
    if not source_ids:
        return {"articles": 0, "days": 0, "undated": 0}
    row = conn.execute(
        f"""
        SELECT count(*) AS articles,
               count(*) FILTER (WHERE published_at IS NULL) AS undated,
               count(DISTINCT {_shanghai_day()}) FILTER (WHERE published_at IS NOT NULL) AS days
        FROM articles
        WHERE source_id = ANY(%s)
        """,
        (source_ids,),
    ).fetchone()
    return {
        "articles": int(row["articles"]),
        "undated": int(row["undated"]),
        "days": int(row["days"]),
    }


def list_day_page(conn, source_ids: list[str], limit: int, offset: int):
    """一页里的几天，以及这些天的全部文章。同一天不会被页边界切开。"""
    if not source_ids:
        return []
    return conn.execute(
        f"""
        WITH days AS (
          SELECT {_shanghai_day()} AS day
          FROM articles
          WHERE source_id = ANY(%s)
            AND published_at IS NOT NULL
          GROUP BY 1
          ORDER BY 1 DESC
          LIMIT %s OFFSET %s
        )
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text
        FROM articles a
        JOIN days d
          ON {_shanghai_day("a.published_at")} = d.day
        WHERE a.source_id = ANY(%s)
        ORDER BY a.published_at DESC, a.id DESC
        """,
        (source_ids, limit, offset, source_ids),
    ).fetchall()


def list_undated_articles(conn, source_ids: list[str], limit: int, offset: int):
    if not source_ids:
        return []
    return conn.execute(
        """
        SELECT id, source_id, title, published_at, body_text
        FROM articles
        WHERE source_id = ANY(%s)
          AND published_at IS NULL
        ORDER BY id DESC
        LIMIT %s OFFSET %s
        """,
        (source_ids, limit, offset),
    ).fetchall()


def source_article_counts(conn, source_ids: list[str]) -> dict[str, int]:
    if not source_ids:
        return {}
    rows = conn.execute(
        """
        SELECT source_id, count(*) AS n
        FROM articles
        WHERE source_id = ANY(%s)
        GROUP BY source_id
        """,
        (source_ids,),
    ).fetchall()
    return {row["source_id"]: int(row["n"]) for row in rows}


def get_article(conn, article_id: int, source_ids: list[str]):
    return conn.execute(
        """
        SELECT id, source_id, title, published_at, body_md, body_text, raw_meta, content_hash
        FROM articles
        WHERE id = %s AND source_id = ANY(%s)
        """,
        (article_id, source_ids),
    ).fetchone()


def article_images(conn, article_id: int):
    return conn.execute(
        """
        SELECT original_ref, storage_path
        FROM article_images
        WHERE article_id = %s
        ORDER BY id
        """,
        (article_id,),
    ).fetchall()


def sibling_articles(conn, source_id: str, article_id: int, parent: str):
    if parent is None:
        return []
    return conn.execute(
        """
        SELECT id, title
        FROM articles
        WHERE source_id = %s AND id <> %s AND raw_meta->>'parent' = %s
        ORDER BY title
        LIMIT 40
        """,
        (source_id, article_id, parent),
    ).fetchall()


def sample_state(conn, source_ids: list[str]) -> bool:
    row = conn.execute(
        """
        SELECT count(*) AS n,
               count(*) FILTER (WHERE raw_meta->>'sample' = 'true') AS samples
        FROM articles
        WHERE source_id = ANY(%s)
        """,
        (source_ids,),
    ).fetchone()
    return int(row["n"]) > 0 and int(row["n"]) == int(row["samples"])


def search_counts(conn, query: str, source_ids: list[str]):
    raw = (query or "").strip()
    if not raw or not source_ids:
        return []
    return conn.execute(
        f"""
        SELECT a.source_id, count(*) AS n
        FROM articles a
        JOIN sources s ON s.id = a.source_id,
             plainto_tsquery('chinese', %(query)s) q
        WHERE a.source_id = ANY(%(ids)s)
          AND {_SEARCH_MATCH}
        GROUP BY a.source_id
        """,
        {"query": raw, "ids": source_ids, "raw": raw},
    ).fetchall()


def search_articles(conn, query: str, source_ids: list[str], source_id: str | None, limit: int = 50):
    raw = (query or "").strip()
    if not raw or not source_ids:
        return []
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at,
               CASE
                 WHEN length(a.body_text) > 20000 THEN left(a.body_text, 20000)
                 ELSE a.body_text
               END AS body_text,
               {_SEARCH_RANK} AS rank
        FROM articles a
        JOIN sources s ON s.id = a.source_id,
             plainto_tsquery('chinese', %(query)s) q
        WHERE a.source_id = ANY(%(ids)s)
          AND (%(source)s::text IS NULL OR a.source_id = %(source)s)
          AND {_SEARCH_MATCH}
        ORDER BY rank DESC, a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %(limit)s
        """,
        {"query": raw, "ids": source_ids, "source": source_id, "raw": raw, "limit": limit},
    ).fetchall()


def list_companies(conn, source_ids: list[str]):
    return conn.execute(
        """
        SELECT c.id, c.ticker, c.name_zh, c.name_en, c.market, c.aliases,
               count(a.id)::int AS mentions
        FROM companies c
        LEFT JOIN article_mentions m ON m.company_id = c.id
        LEFT JOIN articles a ON a.id = m.article_id AND a.source_id = ANY(%s)
        GROUP BY c.id
        ORDER BY mentions DESC, c.name_zh
        """,
        (source_ids,),
    ).fetchall()


def get_company(conn, company_id: int):
    return conn.execute(
        """
        SELECT id, ticker, name_zh, name_en, market, aliases
        FROM companies
        WHERE id = %s
        """,
        (company_id,),
    ).fetchone()


def company_depth_counts(conn, company_id: int, source_ids: list[str]):
    return conn.execute(
        """
        SELECT m.depth, count(*)::int AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s AND a.source_id = ANY(%s)
        GROUP BY m.depth
        """,
        (company_id, source_ids),
    ).fetchall()


def company_months(conn, company_id: int, source_ids: list[str]):
    return conn.execute(
        """
        SELECT to_char(
                 date_trunc('month', a.published_at AT TIME ZONE 'Asia/Shanghai'),
                 'YYYY-MM'
               ) AS ym,
               count(*)::int AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s
          AND a.source_id = ANY(%s)
          AND a.published_at IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """,
        (company_id, source_ids),
    ).fetchall()


def count_company_articles(conn, company_id: int, source_ids: list[str], depth: str | None) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s
          AND a.source_id = ANY(%s)
          AND (%s::text IS NULL OR m.depth = %s)
        """,
        (company_id, source_ids, depth, depth),
    ).fetchone()
    return int(row["n"])


def company_articles(conn, company_id: int, source_ids: list[str], depth: str | None, limit: int, offset: int):
    return conn.execute(
        """
        SELECT a.id, a.title, a.published_at, a.source_id, m.depth, m.hit_count
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s
          AND a.source_id = ANY(%s)
          AND (%s::text IS NULL OR m.depth = %s)
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s OFFSET %s
        """,
        (company_id, source_ids, depth, depth, limit, offset),
    ).fetchall()


def get_translation(conn, article_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT article_id, content_hash, title_zh, body_html, body_text, complete, origin, segments
        FROM article_translations
        WHERE article_id = %s AND lang = 'zh'
        """,
        (article_id,),
    ).fetchone()

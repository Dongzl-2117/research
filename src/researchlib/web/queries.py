"""页面用到的 SQL。只读已启用来源。"""

from __future__ import annotations


def count_articles(conn, source_ids: list[str], source_id: str | None) -> int:
    if source_id:
        row = conn.execute(
            "SELECT count(*) AS n FROM articles WHERE source_id = %s",
            (source_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT count(*) AS n FROM articles WHERE source_id = ANY(%s)",
            (source_ids,),
        ).fetchone()
    return int(row["n"])


def list_articles(conn, source_ids: list[str], source_id: str | None, limit: int, offset: int):
    if source_id:
        return conn.execute(
            """
            SELECT id, source_id, title, published_at, body_text
            FROM articles
            WHERE source_id = %s
            ORDER BY published_at DESC NULLS LAST, id DESC
            LIMIT %s OFFSET %s
            """,
            (source_id, limit, offset),
        ).fetchall()
    return conn.execute(
        """
        SELECT id, source_id, title, published_at, body_text
        FROM articles
        WHERE source_id = ANY(%s)
        ORDER BY published_at DESC NULLS LAST, id DESC
        LIMIT %s OFFSET %s
        """,
        (source_ids, limit, offset),
    ).fetchall()


def get_article(conn, article_id: int, source_ids: list[str]):
    return conn.execute(
        """
        SELECT id, source_id, title, published_at, body_md, body_text, raw_meta
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


def _query_text(conn, query: str) -> str:
    row = conn.execute(
        "SELECT plainto_tsquery('chinese', %s)::text AS q",
        (query,),
    ).fetchone()
    return row["q"] if row else ""


def search_counts(conn, query: str, source_ids: list[str]):
    if not _query_text(conn, query):
        return []
    return conn.execute(
        """
        SELECT source_id, count(*) AS n
        FROM articles, plainto_tsquery('chinese', %s) q
        WHERE search_tsv @@ q AND source_id = ANY(%s)
        GROUP BY source_id
        """,
        (query, source_ids),
    ).fetchall()


def search_articles(conn, query: str, source_ids: list[str], source_id: str | None, limit: int = 50):
    if not _query_text(conn, query):
        return []
    return conn.execute(
        """
        SELECT a.id, a.source_id, a.title, a.published_at,
               CASE
                 WHEN length(a.body_text) > 20000 THEN left(a.body_text, 20000)
                 ELSE a.body_text
               END AS body_text,
               ts_rank_cd(a.search_tsv, q) AS rank
        FROM articles a, plainto_tsquery('chinese', %(query)s) q
        WHERE a.search_tsv @@ q
          AND a.source_id = ANY(%(ids)s)
          AND (%(source)s::text IS NULL OR a.source_id = %(source)s)
        ORDER BY rank DESC, a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %(limit)s
        """,
        {"query": query, "ids": source_ids, "source": source_id, "limit": limit},
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

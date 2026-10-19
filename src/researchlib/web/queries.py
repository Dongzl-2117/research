"""页面用到的 SQL。只读已启用来源。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Scope:
    """检索范围。source_ids 已经按作者或来源收窄过，且只含已启用来源。"""

    source_ids: list[str] = field(default_factory=list)
    company_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    article_id: int | None = None


def scope_clause(scope: Scope, alias: str = "a") -> tuple[str, dict]:
    parts = [f"{alias}.source_id = ANY(%(scope_ids)s)"]
    params: dict = {"scope_ids": list(scope.source_ids)}
    if scope.company_id is not None:
        parts.append(
            f"EXISTS (SELECT 1 FROM article_mentions sm WHERE sm.article_id = {alias}.id"
            " AND sm.company_id = %(scope_company)s)"
        )
        params["scope_company"] = scope.company_id
    if scope.date_from is not None:
        parts.append(f"({alias}.published_at AT TIME ZONE 'Asia/Shanghai')::date >= %(scope_from)s")
        params["scope_from"] = scope.date_from
    if scope.date_to is not None:
        parts.append(f"({alias}.published_at AT TIME ZONE 'Asia/Shanghai')::date <= %(scope_to)s")
        params["scope_to"] = scope.date_to
    if scope.article_id is not None:
        parts.append(f"{alias}.id = %(scope_article)s")
        params["scope_article"] = scope.article_id
    return " AND ".join(parts), params


_CLOCK_FIELDS = """
a.raw_meta->>'create_time' AS clock_hint,
a.raw_meta->>'date_source' AS date_source,
a.raw_meta->>'time_known' AS time_known
"""


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
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text,
               {_CLOCK_FIELDS}
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


def search_counts(conn, query: str, scope: Scope):
    raw = (query or "").strip()
    if not raw or not scope.source_ids:
        return []
    where, params = scope_clause(scope)
    return conn.execute(
        f"""
        SELECT a.source_id, count(*) AS n
        FROM articles a
        JOIN sources s ON s.id = a.source_id,
             websearch_to_tsquery('chinese', %(query)s) q
        WHERE {where}
          AND {_SEARCH_MATCH}
        GROUP BY a.source_id
        """,
        {"query": raw, "raw": raw, **params},
    ).fetchall()


def search_articles(conn, query: str, scope: Scope, limit: int = 20, offset: int = 0):
    raw = (query or "").strip()
    if not raw or not scope.source_ids:
        return []
    where, params = scope_clause(scope)
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at,
               left(a.body_text, 20000) AS body_text,
               {_SEARCH_RANK} AS rank
        FROM articles a
        JOIN sources s ON s.id = a.source_id,
             websearch_to_tsquery('chinese', %(query)s) q
        WHERE {where}
          AND {_SEARCH_MATCH}
        ORDER BY rank DESC, a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"query": raw, "raw": raw, "limit": limit, "offset": offset, **params},
    ).fetchall()


def best_chunks(conn, query: str, article_ids: list[int]) -> dict[int, dict]:
    """每篇文章里和检索词最相关的一块，用来做检索结果的片段。"""
    if not article_ids or not (query or "").strip():
        return {}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (c.article_id) c.article_id, c.ord, c.heading, c.body
        FROM article_chunks c, websearch_to_tsquery('chinese', %(query)s) q
        WHERE c.article_id = ANY(%(ids)s) AND c.search_tsv @@ q
        ORDER BY c.article_id, ts_rank_cd(c.search_tsv, q) DESC, c.ord
        """,
        {"query": query, "ids": article_ids},
    ).fetchall()
    return {row["article_id"]: row for row in rows}


def scoped_articles(conn, scope: Scope, limit: int, offset: int = 0):
    """不带检索词时，范围内的文章按时间倒序。给 JSON 接口用。"""
    if not scope.source_ids:
        return []
    where, params = scope_clause(scope)
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at, left(a.body_text, 2000) AS body_text
        FROM articles a
        WHERE {where}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"limit": limit, "offset": offset, **params},
    ).fetchall()


def count_scoped_articles(conn, scope: Scope) -> int:
    if not scope.source_ids:
        return 0
    where, params = scope_clause(scope)
    row = conn.execute(f"SELECT count(*) AS n FROM articles a WHERE {where}", params).fetchone()
    return int(row["n"])


def article_tags(conn, article_ids: list[int], per_article: int = 4) -> dict[int, list[dict]]:
    """公司标签。一带而过的不上卡片。aliases 留给卡片再筛掉开头没谈的公司。"""
    if not article_ids:
        return {}
    rows = conn.execute(
        """
        SELECT m.article_id, c.id, c.name_zh, c.name_en, c.aliases, m.depth, m.hit_count
        FROM article_mentions m
        JOIN companies c ON c.id = m.company_id
        WHERE m.article_id = ANY(%s) AND m.depth <> 'passing'
        ORDER BY m.article_id,
                 CASE m.depth WHEN 'title' THEN 0 WHEN 'whole' THEN 1 ELSE 2 END,
                 m.hit_count DESC, c.name_zh
        """,
        (article_ids,),
    ).fetchall()
    found: dict[int, list[dict]] = {}
    for row in rows:
        items = found.setdefault(row["article_id"], [])
        if len(items) < per_article:
            items.append(
                {
                    "id": row["id"],
                    "name": row["name_zh"],
                    "name_en": row["name_en"] or "",
                    "aliases": list(row["aliases"] or []),
                    "depth": row["depth"],
                }
            )
    return found


def article_mentions(conn, article_id: int):
    return conn.execute(
        """
        SELECT c.id, c.name_zh, c.ticker, c.market, m.depth, m.hit_count
        FROM article_mentions m
        JOIN companies c ON c.id = m.company_id
        WHERE m.article_id = %s
        ORDER BY CASE m.depth WHEN 'title' THEN 0 WHEN 'whole' THEN 1 WHEN 'section' THEN 2 ELSE 3 END,
                 m.hit_count DESC, c.name_zh
        """,
        (article_id,),
    ).fetchall()


def digests_for(conn, article_ids: list[int]) -> dict[int, dict]:
    """只取和当前正文对得上的摘要。正文改过而摘要没重算的，当作没有。"""
    if not article_ids:
        return {}
    rows = conn.execute(
        """
        SELECT d.article_id, d.summary, d.points, d.tags, d.model
        FROM article_digests d
        JOIN articles a ON a.id = d.article_id AND a.content_hash = d.content_hash
        WHERE d.article_id = ANY(%s)
        """,
        (article_ids,),
    ).fetchall()
    return {row["article_id"]: row for row in rows}


def article_chunks(conn, article_id: int):
    return conn.execute(
        "SELECT ord, heading, body FROM article_chunks WHERE article_id = %s ORDER BY ord",
        (article_id,),
    ).fetchall()


def source_stats(conn, source_ids: list[str]) -> dict[str, dict]:
    if not source_ids:
        return {}
    rows = conn.execute(
        """
        SELECT source_id, count(*)::int AS n, min(published_at) AS first_at, max(published_at) AS last_at
        FROM articles
        WHERE source_id = ANY(%s)
        GROUP BY source_id
        """,
        (source_ids,),
    ).fetchall()
    return {row["source_id"]: row for row in rows}


def latest_articles(conn, source_ids: list[str], limit: int = 3):
    if not source_ids:
        return []
    return conn.execute(
        """
        SELECT id, source_id, title, published_at
        FROM articles
        WHERE source_id = ANY(%s)
        ORDER BY published_at DESC NULLS LAST, id DESC
        LIMIT %s
        """,
        (source_ids, limit),
    ).fetchall()


def top_companies(conn, source_ids: list[str], limit: int = 12):
    """一组来源最常深入谈到的公司。一带而过不计。"""
    if not source_ids:
        return []
    return conn.execute(
        """
        SELECT c.id, c.name_zh, count(*)::int AS n, max(a.published_at) AS last_at
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        JOIN companies c ON c.id = m.company_id
        WHERE a.source_id = ANY(%s) AND m.depth <> 'passing'
        GROUP BY c.id, c.name_zh
        ORDER BY n DESC, last_at DESC NULLS LAST, c.name_zh
        LIMIT %s
        """,
        (source_ids, limit),
    ).fetchall()


def matching_companies(conn, query: str, limit: int = 6):
    """检索词正好是某家公司的名字、代码或简称时，结果页顶部直达公司页。"""
    raw = (query or "").strip()
    if not raw:
        return []
    return conn.execute(
        """
        SELECT id, name_zh, ticker, market
        FROM companies
        WHERE name_zh = %(q)s
           OR lower(name_en) = lower(%(q)s)
           OR lower(coalesce(ticker, '')) = lower(%(q)s)
           OR %(q)s = ANY(aliases)
           OR (char_length(%(q)s) >= 2 AND position(%(q)s in name_zh) > 0)
        ORDER BY (name_zh = %(q)s) DESC, name_zh
        LIMIT %(limit)s
        """,
        {"q": raw, "limit": limit},
    ).fetchall()


def day_offset(conn, source_ids: list[str], day) -> int:
    """比 day 新的有日期的天数。用来把「跳到某天」换成页码。"""
    if not source_ids:
        return 0
    row = conn.execute(
        f"""
        SELECT count(DISTINCT {_shanghai_day()}) AS n
        FROM articles
        WHERE source_id = ANY(%s)
          AND published_at IS NOT NULL
          AND (published_at AT TIME ZONE 'Asia/Shanghai')::date > %s
        """,
        (source_ids, day),
    ).fetchone()
    return int(row["n"])


def nearest_day(conn, source_ids: list[str], day):
    """不晚于 day 的最近一个有文章的日子。"""
    if not source_ids:
        return None
    row = conn.execute(
        """
        SELECT max((published_at AT TIME ZONE 'Asia/Shanghai')::date) AS d
        FROM articles
        WHERE source_id = ANY(%s)
          AND published_at IS NOT NULL
          AND (published_at AT TIME ZONE 'Asia/Shanghai')::date <= %s
        """,
        (source_ids, day),
    ).fetchone()
    return row["d"]


def articles_on_day(conn, scope: Scope):
    """某一天、当前范围内的文章。带上钟点线索，卡片才能把 00:00 还原成真实时分。"""
    if not scope.source_ids:
        return []
    where, params = scope_clause(scope)
    return conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text,
               {_CLOCK_FIELDS}
        FROM articles a
        WHERE {where}
          AND a.published_at IS NOT NULL
        ORDER BY a.published_at DESC, a.id DESC
        """,
        params,
    ).fetchall()


def neighbor_days(conn, scope: Scope, day: date) -> tuple[date | None, date | None]:
    """同一范围内，这一天的前一个和后一个有文章的日子。"""
    bare = Scope(source_ids=list(scope.source_ids), company_id=scope.company_id)
    if not bare.source_ids:
        return None, None
    where, params = scope_clause(bare)
    previous = conn.execute(
        f"""
        SELECT max((a.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS d
        FROM articles a
        WHERE {where}
          AND a.published_at IS NOT NULL
          AND (a.published_at AT TIME ZONE 'Asia/Shanghai')::date < %(day)s
        """,
        {**params, "day": day},
    ).fetchone()
    following = conn.execute(
        f"""
        SELECT min((a.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS d
        FROM articles a
        WHERE {where}
          AND a.published_at IS NOT NULL
          AND (a.published_at AT TIME ZONE 'Asia/Shanghai')::date > %(day)s
        """,
        {**params, "day": day},
    ).fetchone()
    return previous["d"], following["d"]


def day_answer(conn, day: date, source_ids: list[str], company_id: int | None):
    """已经问过的、正好覆盖这一天和同一范围的回答。没有就不现算。"""
    rows = conn.execute(
        """
        SELECT id, question, answer, citations, model, scope, created_at
        FROM llm_answers
        WHERE scope->>'date_from' = %s AND scope->>'date_to' = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 30
        """,
        (day.isoformat(), day.isoformat()),
    ).fetchall()
    wanted = set(source_ids)
    for row in rows:
        scope = row["scope"] or {}
        saved_company = scope.get("company_id")
        if company_id is None and saved_company:
            continue
        if company_id is not None and saved_company != company_id:
            continue
        if set(scope.get("source_ids") or []) != wanted:
            continue
        return row
    return None


def day_articles(conn, source_ids: list[str], date_from, date_to, limit: int = 500):
    if not source_ids:
        return []
    return conn.execute(
        """
        SELECT id, source_id, title, published_at, left(body_text, 2000) AS body_text
        FROM articles
        WHERE source_id = ANY(%s)
          AND published_at IS NOT NULL
          AND (published_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
        ORDER BY published_at DESC, id DESC
        LIMIT %s
        """,
        (source_ids, date_from, date_to, limit),
    ).fetchall()


def company_author_counts(conn, company_id: int, source_ids: list[str]):
    return conn.execute(
        """
        SELECT a.source_id, count(*)::int AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s AND a.source_id = ANY(%s)
        GROUP BY a.source_id
        """,
        (company_id, source_ids),
    ).fetchall()


def recent_answers(conn, limit: int = 20):
    return conn.execute(
        "SELECT id, question, scope, created_at FROM llm_answers ORDER BY created_at DESC, id DESC LIMIT %s",
        (limit,),
    ).fetchall()


def get_answer(conn, answer_id: int):
    return conn.execute(
        "SELECT id, question, scope, answer, citations, model, created_at FROM llm_answers WHERE id = %s",
        (answer_id,),
    ).fetchone()


def list_companies(conn, source_ids: list[str]):
    return conn.execute(
        """
        SELECT c.id, c.ticker, c.name_zh, c.name_en, c.market, c.aliases,
               count(a.id)::int AS mentions,
               count(a.id) FILTER (WHERE m.depth <> 'passing')::int AS deep,
               max(a.published_at) AS last_at
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
        f"""
        SELECT a.id, a.title, a.published_at, a.source_id, m.depth, m.hit_count,
               left(a.body_text, 20000) AS body_text,
               {_CLOCK_FIELDS}
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

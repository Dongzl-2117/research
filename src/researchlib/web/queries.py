"""页面用到的 SQL。只读已启用来源。"""

from __future__ import annotations

import json
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
    if scope.article_id is None:
        # 同一篇的重复只留主版本。指定了某一篇（「只问这篇」）时照常。
        parts.append(f"{alias}.duplicate_of IS NULL")
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
a.raw_meta->>'time_known' AS time_known,
a.raw_meta->>'poster_name' AS poster_name,
a.raw_meta->>'poster_role' AS poster_role
"""


def _owner_clause(owner_only: bool, alias: str = "") -> str:
    """作者统计和作者页只算星主原创。网友帖留在来源时间线里，但不记到星主名下。"""
    if not owner_only:
        return ""
    column = f"{alias}.raw_meta" if alias else "raw_meta"
    return f" AND coalesce({column}->>'poster_role', '') <> 'guest'"


def _shanghai_day(column: str = "published_at") -> str:
    # 与 company_months 一样，按 Asia/Shanghai 的日历日切桶。
    return f"date_trunc('day', {column} AT TIME ZONE 'Asia/Shanghai')"

def _off_timeline_clause(include_off_timeline: bool, alias: str = "") -> str:
    """没日期的段永平条目标了 off_timeline。首页按天不收；作者页要收。"""
    if include_off_timeline:
        return ""
    column = f"{alias}.raw_meta" if alias else "raw_meta"
    return f" AND coalesce({column}->>'off_timeline', '') <> 'true'"


def feed_counts(conn, source_ids: list[str], *, include_off_timeline: bool = True, owner_only: bool = False) -> dict:
    if not source_ids:
        return {"articles": 0, "days": 0, "undated": 0}
    extra = _off_timeline_clause(include_off_timeline) + _owner_clause(owner_only)
    row = conn.execute(
        f"""
        SELECT count(*) AS articles,
               count(*) FILTER (WHERE published_at IS NULL) AS undated,
               count(DISTINCT {_shanghai_day()}) FILTER (WHERE published_at IS NOT NULL) AS days
        FROM articles
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
        {extra}
        """,
        (source_ids,),
    ).fetchone()
    return {
        "articles": int(row["articles"]),
        "undated": int(row["undated"]),
        "days": int(row["days"]),
    }


def list_day_page(
    conn, source_ids: list[str], limit: int, offset: int, *, include_off_timeline: bool = True, owner_only: bool = False
):
    """一页里的几天，以及这些天的全部文章。同一天不会被页边界切开。"""
    if not source_ids:
        return []
    extra = _off_timeline_clause(include_off_timeline) + _owner_clause(owner_only)
    extra_a = _off_timeline_clause(include_off_timeline, "a") + _owner_clause(owner_only, "a")
    return conn.execute(
        f"""
        WITH days AS (
          SELECT {_shanghai_day()} AS day
          FROM articles
          WHERE source_id = ANY(%s) AND duplicate_of IS NULL
            AND published_at IS NOT NULL
            {extra}
          GROUP BY 1
          ORDER BY 1 DESC
          LIMIT %s OFFSET %s
        )
        SELECT a.id, a.source_id, a.title, a.published_at, a.body_text,
               {_CLOCK_FIELDS}
        FROM articles a
        JOIN days d
          ON {_shanghai_day("a.published_at")} = d.day
        WHERE a.source_id = ANY(%s) AND a.duplicate_of IS NULL
          {extra_a}
        ORDER BY a.published_at DESC, a.id DESC
        """,
        (source_ids, limit, offset, source_ids),
    ).fetchall()


def list_undated_articles(
    conn,
    source_ids: list[str],
    limit: int,
    offset: int,
    *,
    include_off_timeline: bool = True,
    owner_only: bool = False,
):
    if not source_ids:
        return []
    extra = _off_timeline_clause(include_off_timeline) + _owner_clause(owner_only)
    return conn.execute(
        f"""
        SELECT id, source_id, title, published_at, body_text,
               raw_meta->>'poster_name' AS poster_name,
               raw_meta->>'poster_role' AS poster_role
        FROM articles
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
          AND published_at IS NULL
          {extra}
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
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
        GROUP BY source_id
        """,
        (source_ids,),
    ).fetchall()
    return {row["source_id"]: int(row["n"]) for row in rows}


def get_article(conn, article_id: int, source_ids: list[str]):
    return conn.execute(
        """
        SELECT id, source_id, title, published_at, body_md, body_text, raw_meta, content_hash,
               duplicate_of, ocr_hash
        FROM articles
        WHERE id = %s AND source_id = ANY(%s)
        """,
        (article_id, source_ids),
    ).fetchone()


def same_article_elsewhere(conn, article_id: int, duplicate_of: int | None, source_ids: list[str]):
    """同一篇的其他份：这篇是重复时给主版本，是主版本时给指向它的重复。"""
    if duplicate_of is not None:
        return conn.execute(
            "SELECT id, source_id, true AS canonical FROM articles WHERE id = %s AND source_id = ANY(%s)",
            (duplicate_of, source_ids),
        ).fetchall()
    return conn.execute(
        """
        SELECT id, source_id, false AS canonical FROM articles
        WHERE duplicate_of = %s AND source_id = ANY(%s)
        ORDER BY id
        """,
        (article_id, source_ids),
    ).fetchall()


def count_duplicate_groups(conn, source_ids: list[str], only: int | None = None) -> int:
    row = conn.execute(
        """
        SELECT count(DISTINCT d.duplicate_of) AS n
        FROM articles d JOIN articles c ON c.id = d.duplicate_of
        WHERE d.source_id = ANY(%s) AND c.source_id = ANY(%s) AND (%s::bigint IS NULL OR c.id = %s::bigint)
        """,
        (source_ids, source_ids, only, only),
    ).fetchone()
    return int(row["n"])


def duplicate_groups(conn, source_ids: list[str], limit: int, offset: int, only: int | None = None) -> list[dict]:
    """去重核对页：每组一个主版本和指向它的重复，最近认出的在前。"""
    heads = conn.execute(
        """
        SELECT c.id, c.title, c.source_id, c.published_at, c.body_text, max(d.duplicate_marked_at) AS marked
        FROM articles d JOIN articles c ON c.id = d.duplicate_of
        WHERE d.source_id = ANY(%s) AND c.source_id = ANY(%s) AND (%s::bigint IS NULL OR c.id = %s::bigint)
        GROUP BY c.id
        ORDER BY max(d.duplicate_marked_at) DESC NULLS LAST, c.published_at DESC NULLS LAST, c.id DESC
        LIMIT %s OFFSET %s
        """,
        (source_ids, source_ids, only, only, limit, offset),
    ).fetchall()
    if not heads:
        return []
    members: dict[int, list] = {}
    for row in conn.execute(
        """
        SELECT id, title, source_id, published_at, body_text, duplicate_of, duplicate_marked_at
        FROM articles
        WHERE duplicate_of = ANY(%s) AND source_id = ANY(%s)
        ORDER BY id
        """,
        ([head["id"] for head in heads], source_ids),
    ).fetchall():
        members.setdefault(row["duplicate_of"], []).append(row)
    return [{"canonical": head, "duplicates": members.get(head["id"], [])} for head in heads]


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


def image_ocr_texts(conn, article_id: int) -> dict[str, str]:
    """这篇本地图已经识别出的字。按内容哈希，阅读页现查，新识别的刷新就能看见。"""
    rows = conn.execute(
        """
        SELECT i.content_hash, t.text
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        WHERE i.article_id = %s AND btrim(t.text) <> ''
        """,
        (article_id,),
    ).fetchall()
    return {row["content_hash"]: row["text"] or "" for row in rows}


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
    """只取和当前正文对得上的摘要。正文改过而摘要没重算的，当作没有。提示词版本旧的照常返回，调用方再排队重写。"""
    if not article_ids:
        return {}
    rows = conn.execute(
        """
        SELECT d.article_id, d.summary, d.points, d.tags, d.model, d.prompt_version, d.head_only, d.ocr_hash
        FROM article_digests d
        JOIN articles a ON a.id = d.article_id AND a.content_hash = d.content_hash
        WHERE d.article_id = ANY(%s)
        """,
        (article_ids,),
    ).fetchall()
    return {row["article_id"]: row for row in rows}


def article_chunks(conn, article_id: int):
    return conn.execute(
        "SELECT ord, heading, body, lang, anchor FROM article_chunks WHERE article_id = %s ORDER BY ord",
        (article_id,),
    ).fetchall()


def source_stats(conn, source_ids: list[str]) -> dict[str, dict]:
    if not source_ids:
        return {}
    rows = conn.execute(
        """
        SELECT source_id, count(*)::int AS n, min(published_at) AS first_at, max(published_at) AS last_at
        FROM articles
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
          AND coalesce(raw_meta->>'poster_role', '') <> 'guest'
        GROUP BY source_id
        """,
        (source_ids,),
    ).fetchall()
    return {row["source_id"]: row for row in rows}


def recent_publish_times(conn, source_ids: list[str], per_source: int = 31) -> dict[str, list]:
    """每个来源最近几十篇的发布时间，新的在前。用来看平时多久发一篇、现在是不是断更了。"""
    if not source_ids:
        return {}
    rows = conn.execute(
        """
        SELECT source_id, published_at FROM (
          SELECT source_id, published_at,
                 row_number() OVER (PARTITION BY source_id ORDER BY published_at DESC) AS rn
          FROM articles
          WHERE source_id = ANY(%s) AND duplicate_of IS NULL AND published_at IS NOT NULL
            AND published_at <= now() + interval '1 day'
        ) recent
        WHERE rn <= %s
        ORDER BY source_id, published_at DESC
        """,
        (source_ids, per_source),
    ).fetchall()
    found: dict[str, list] = {}
    for row in rows:
        found.setdefault(row["source_id"], []).append(row["published_at"])
    return found


def last_imports(conn, source_ids: list[str]) -> dict[str, dict]:
    """每个来源最近一次导入：{来源: {"at": 时间, "origin": "aihot" | "archive"}}。

    导入命令写进 app_state 的 import:<来源>。早先的记录没有 origin，当作手动导入的归档。
    """
    if not source_ids:
        return {}
    rows = conn.execute(
        "SELECT key, value, updated_at FROM app_state WHERE key = ANY(%s)",
        (["import:" + source_id for source_id in source_ids],),
    ).fetchall()
    found: dict[str, dict] = {}
    for row in rows:
        try:
            value = json.loads(row["value"] or "{}")
        except ValueError:
            value = {}
        origin = value.get("origin") if isinstance(value, dict) else None
        found[row["key"].split(":", 1)[1]] = {"at": row["updated_at"], "origin": origin or "archive"}
    return found


def latest_articles(conn, source_ids: list[str], limit: int = 3):
    if not source_ids:
        return []
    return conn.execute(
        """
        SELECT id, source_id, title, published_at
        FROM articles
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
          AND coalesce(raw_meta->>'poster_role', '') <> 'guest'
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
          AND coalesce(a.raw_meta->>'poster_role', '') <> 'guest'
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


def day_offset(conn, source_ids: list[str], day, *, owner_only: bool = False) -> int:
    """比 day 新的有日期的天数。用来把「跳到某天」换成页码。"""
    if not source_ids:
        return 0
    row = conn.execute(
        f"""
        SELECT count(DISTINCT {_shanghai_day()}) AS n
        FROM articles
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
          AND published_at IS NOT NULL
          AND (published_at AT TIME ZONE 'Asia/Shanghai')::date > %s
          {_owner_clause(owner_only)}
        """,
        (source_ids, day),
    ).fetchone()
    return int(row["n"])


def nearest_day(conn, source_ids: list[str], day, *, owner_only: bool = False):
    """不晚于 day 的最近一个有文章的日子。"""
    if not source_ids:
        return None
    row = conn.execute(
        f"""
        SELECT max((published_at AT TIME ZONE 'Asia/Shanghai')::date) AS d
        FROM articles
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
          AND published_at IS NOT NULL
          AND (published_at AT TIME ZONE 'Asia/Shanghai')::date <= %s
          {_owner_clause(owner_only)}
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
        WHERE scope->>'date_from' = %s AND scope->>'date_to' = %s AND status = 'done'
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
        WHERE source_id = ANY(%s) AND duplicate_of IS NULL
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
        "SELECT id, question, scope, status, created_at FROM llm_answers ORDER BY created_at DESC, id DESC LIMIT %s",
        (limit,),
    ).fetchall()


def get_answer(conn, answer_id: int):
    return conn.execute(
        """
        SELECT id, question, scope, answer, citations, model, status, error, terms, stats, created_at, updated_at
        FROM llm_answers
        WHERE id = %s
        """,
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
        SELECT id, ticker, name_zh, name_en, market, aliases, alias_rules
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


def _depth_filter(depth: str | None) -> tuple[str, tuple]:
    """depth：None 全部；'deep' 标题、全文、单节；其余是某一种深度。"""
    if depth is None:
        return "", ()
    if depth == "deep":
        return " AND m.depth <> 'passing'", ()
    return " AND m.depth = %s", (depth,)


def count_company_articles(conn, company_id: int, source_ids: list[str], depth: str | None) -> int:
    clause, extra = _depth_filter(depth)
    row = conn.execute(
        f"""
        SELECT count(*) AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s
          AND a.source_id = ANY(%s){clause}
        """,
        (company_id, source_ids, *extra),
    ).fetchone()
    return int(row["n"])


def company_articles(conn, company_id: int, source_ids: list[str], depth: str | None, limit: int, offset: int):
    clause, extra = _depth_filter(depth)
    return conn.execute(
        f"""
        SELECT a.id, a.title, a.published_at, a.source_id, m.depth, m.hit_count,
               left(a.body_text, 20000) AS body_text,
               {_CLOCK_FIELDS}
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s
          AND a.source_id = ANY(%s){clause}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s OFFSET %s
        """,
        (company_id, source_ids, *extra, limit, offset),
    ).fetchall()


def company_month_depths(conn, company_id: int, source_ids: list[str]):
    """按月：深入（标题、全文、单节）和一带而过各几篇。图表用。"""
    return conn.execute(
        """
        SELECT to_char(date_trunc('month', a.published_at AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM') AS ym,
               count(*) FILTER (WHERE m.depth <> 'passing')::int AS deep,
               count(*) FILTER (WHERE m.depth = 'passing')::int AS passing
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s AND a.source_id = ANY(%s) AND a.published_at IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """,
        (company_id, source_ids),
    ).fetchall()


def company_source_stats(conn, company_id: int, source_ids: list[str]):
    """每个来源谈这家公司：深入几篇、一共几篇、最近一次深入。"""
    return conn.execute(
        """
        SELECT a.source_id,
               count(*)::int AS total,
               count(*) FILTER (WHERE m.depth <> 'passing')::int AS deep,
               max(a.published_at) FILTER (WHERE m.depth <> 'passing') AS last_deep,
               max(a.published_at) AS last_at
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = %s AND a.source_id = ANY(%s)
          AND coalesce(a.raw_meta->>'poster_role', '') <> 'guest'
        GROUP BY a.source_id
        """,
        (company_id, source_ids),
    ).fetchall()


def company_cards(conn, company_ids: list[int], source_ids: list[str], months: int = 24) -> dict[int, dict]:
    """持仓和观察卡片：深入篇数、全部篇数、最近一次深入的文章、近几个月按月深入篇数、各来源深入篇数。"""
    if not company_ids or not source_ids:
        return {}
    cards: dict[int, dict] = {
        company_id: {"deep": 0, "total": 0, "latest": None, "months": [], "by_source": {}}
        for company_id in company_ids
    }
    for row in conn.execute(
        """
        SELECT m.company_id, a.source_id,
               count(*)::int AS total,
               count(*) FILTER (WHERE m.depth <> 'passing')::int AS deep
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = ANY(%s) AND a.source_id = ANY(%s)
        GROUP BY m.company_id, a.source_id
        """,
        (company_ids, source_ids),
    ).fetchall():
        card = cards[row["company_id"]]
        card["total"] += row["total"]
        card["deep"] += row["deep"]
        if row["deep"]:
            card["by_source"][row["source_id"]] = row["deep"]
    for row in conn.execute(
        """
        SELECT DISTINCT ON (m.company_id) m.company_id, a.id, a.title, a.published_at, a.source_id, m.depth
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = ANY(%s) AND a.source_id = ANY(%s)
          AND m.depth <> 'passing' AND a.published_at IS NOT NULL
        ORDER BY m.company_id, a.published_at DESC, a.id DESC
        """,
        (company_ids, source_ids),
    ).fetchall():
        cards[row["company_id"]]["latest"] = row
    for row in conn.execute(
        """
        SELECT m.company_id,
               to_char(date_trunc('month', a.published_at AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM') AS ym,
               count(*)::int AS deep
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = ANY(%s) AND a.source_id = ANY(%s) AND m.depth <> 'passing'
          AND a.published_at >= date_trunc('month', now()) - make_interval(months => %s)
        GROUP BY 1, 2
        """,
        (company_ids, source_ids, months - 1),
    ).fetchall():
        cards[row["company_id"]]["months"].append({"ym": row["ym"], "deep": row["deep"], "passing": 0})
    return cards


def recent_viewed_companies(conn, source_ids: list[str], limit: int = 8) -> list[dict]:
    """有作者判断的公司，按最近一条观点的发布时间从新到旧。不按持仓过滤。"""
    if not source_ids or limit <= 0:
        return []
    return conn.execute(
        """
        SELECT id, name_zh, published_at
        FROM (
          SELECT DISTINCT ON (c.id) c.id, c.name_zh, a.published_at
          FROM company_views v
          JOIN articles a ON a.id = v.article_id
          JOIN companies c ON c.id = v.company_id
          WHERE v.about AND v.judgment <> '' AND a.duplicate_of IS NULL AND a.source_id = ANY(%s)
            AND coalesce(a.raw_meta->>'poster_role', '') <> 'guest'
          ORDER BY c.id, a.published_at DESC NULLS LAST, a.id DESC
        ) recent
        ORDER BY published_at DESC NULLS LAST, id DESC
        LIMIT %s
        """,
        (source_ids, limit),
    ).fetchall()


def holdings_recent(conn, company_ids: list[int], source_ids: list[str], days: int = 30) -> dict[int, dict]:
    """每家最近一篇深入谈到它的文章，和近 days 天里深入谈到它的篇数。"""
    if not company_ids or not source_ids:
        return {}
    found: dict[int, dict] = {company_id: {"latest": None, "recent": 0} for company_id in company_ids}
    for row in conn.execute(
        """
        SELECT DISTINCT ON (m.company_id) m.company_id, a.id, a.title, a.published_at, a.source_id, m.depth
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = ANY(%s) AND a.source_id = ANY(%s)
          AND m.depth <> 'passing' AND a.published_at IS NOT NULL
        ORDER BY m.company_id, a.published_at DESC, a.id DESC
        """,
        (company_ids, source_ids),
    ).fetchall():
        found[row["company_id"]]["latest"] = row
    for row in conn.execute(
        """
        SELECT m.company_id, count(*)::int AS n
        FROM article_mentions m
        JOIN articles a ON a.id = m.article_id
        WHERE m.company_id = ANY(%s) AND a.source_id = ANY(%s)
          AND m.depth <> 'passing' AND a.published_at >= now() - make_interval(days => %s)
        GROUP BY m.company_id
        """,
        (company_ids, source_ids, days),
    ).fetchall():
        found[row["company_id"]]["recent"] = row["n"]
    return found


def companies_by_name(conn, names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    rows = conn.execute(
        "SELECT id, ticker, name_zh, name_en, market, aliases FROM companies WHERE name_zh = ANY(%s)",
        (names,),
    ).fetchall()
    return {row["name_zh"]: row for row in rows}


def get_translation(conn, article_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT article_id, content_hash, title_zh, body_html, body_text, complete, origin, segments
        FROM article_translations
        WHERE article_id = %s AND lang = 'zh'
        """,
        (article_id,),
    ).fetchone()

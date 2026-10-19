"""给问答找资料：从问题里认出公司和作者，再在段落块上召回。

自然语言问题直接做 AND 全文检索几乎总是零结果（「怎么看」「最近」这类词不在正文里），
所以这里把问题分词后用 OR 连起来，靠 ts_rank_cd 排序；问题里认出的公司再按别名
补一路召回。最后每篇文章最多取几块，避免一篇长文占满上下文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from researchlib.mentions import CompanyMatcher, iter_alias_specs, load_company_rows
from researchlib.web.queries import Scope, scope_clause

# 问句里常见、但对召回没有帮助的词。
STOPWORDS = frozenset(
    {
        "什么", "怎么", "怎么样", "如何", "哪些", "为什么", "是否", "有没有", "看法", "观点", "认为",
        "最近", "近期", "最新", "目前", "现在", "这个", "那个", "他们", "我们", "你们", "一下",
        "总结", "汇总", "整合", "介绍", "分析", "一些", "以及", "还是", "可以", "应该", "关于",
        "文章", "作者", "提到", "说过", "讲过", "谈到", "the", "and", "what", "how",
    }
)
RECENT_WORDS = ("最近", "近期", "最新", "目前", "现在", "这周", "本周", "今年")
MAX_PER_ARTICLE = 3
MAX_PASSAGES = 14
CHAR_BUDGET = 9000


@dataclass
class Passage:
    article_id: int
    ord: int
    heading: str
    body: str
    title: str
    source_id: str
    published_at: object
    score: float = 0.0


@dataclass
class Retrieval:
    passages: list[Passage]
    companies: list[dict]
    authors: list[str]
    scope: Scope


def _tsquery_text(conn, question: str) -> str:
    rows = conn.execute(
        "SELECT DISTINCT lexeme FROM unnest(to_tsvector('chinese', %s))",
        (question,),
    ).fetchall()
    terms = []
    for row in rows:
        lexeme = row["lexeme"]
        if len(lexeme) < 2 or lexeme in STOPWORDS:
            continue
        terms.append("'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'")
    return " | ".join(sorted(terms))


def detect_companies(conn, question: str) -> list[dict]:
    rows = load_company_rows(conn)
    if not rows:
        return []
    counts = CompanyMatcher(rows).count(question)
    by_id = {int(row["id"]): row for row in rows}
    return [dict(by_id[company_id]) for company_id in counts]


def detect_authors(question: str, sources) -> list[str]:
    found: list[str] = []
    for source in sources:
        if source.author and source.author in question and source.author not in found:
            found.append(source.author)
    return found


def _rows_to_passages(rows, score_key: str | None = "rank") -> list[Passage]:
    return [
        Passage(
            article_id=row["article_id"],
            ord=row["ord"],
            heading=row["heading"],
            body=row["body"],
            title=row["title"],
            source_id=row["source_id"],
            published_at=row["published_at"],
            score=float(row[score_key]) if score_key else 0.0,
        )
        for row in rows
    ]


_SELECT = """
SELECT c.article_id, c.ord, c.heading, c.body, a.title, a.source_id, a.published_at
"""


def _fts(conn, tsq: str, scope: Scope, limit: int) -> list[Passage]:
    if not tsq:
        return []
    where, params = scope_clause(scope)
    rows = conn.execute(
        _SELECT
        + f""", ts_rank_cd(c.search_tsv, q) AS rank
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id,
             to_tsquery('chinese', %(tsq)s) q
        WHERE {where} AND c.search_tsv @@ q
        ORDER BY rank DESC, a.published_at DESC NULLS LAST, c.ord
        LIMIT %(limit)s
        """,
        {"tsq": tsq, "limit": limit, **params},
    ).fetchall()
    return _rows_to_passages(rows)


def _alias_hits(conn, company: dict, scope: Scope, limit: int) -> list[Passage]:
    patterns = ["%" + text.replace("%", r"\%").replace("_", r"\_") + "%" for text, _ in iter_alias_specs(company)]
    if not patterns:
        return []
    where, params = scope_clause(replace(scope, company_id=int(company["id"])))
    rows = conn.execute(
        _SELECT
        + f"""
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        WHERE {where} AND c.body ILIKE ANY(%(patterns)s)
        ORDER BY a.published_at DESC NULLS LAST, c.ord
        LIMIT %(limit)s
        """,
        {"patterns": patterns, "limit": limit, **params},
    ).fetchall()
    return _rows_to_passages(rows, None)


def _leading(conn, scope: Scope, limit: int) -> list[Passage]:
    """没有可用检索词时（例如「汇总这一天」），取范围内每篇文章的开头几块。"""
    where, params = scope_clause(scope)
    rows = conn.execute(
        _SELECT
        + f"""
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        WHERE {where} AND c.ord < 2
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC, c.ord
        LIMIT %(limit)s
        """,
        {"limit": limit, **params},
    ).fetchall()
    return _rows_to_passages(rows, None)


def _article_in_order(conn, scope: Scope) -> list[Passage]:
    where, params = scope_clause(scope)
    rows = conn.execute(
        _SELECT
        + f"""
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        WHERE {where}
        ORDER BY c.ord
        LIMIT 40
        """,
        params,
    ).fetchall()
    return _rows_to_passages(rows, None)


def _pick(candidates: list[Passage], *, per_article: int, limit: int, budget: int) -> list[Passage]:
    picked: list[Passage] = []
    seen: set[tuple[int, int]] = set()
    per: dict[int, int] = {}
    used = 0
    for passage in candidates:
        key = (passage.article_id, passage.ord)
        if key in seen or per.get(passage.article_id, 0) >= per_article:
            continue
        if picked and used + len(passage.body) > budget:
            continue
        seen.add(key)
        per[passage.article_id] = per.get(passage.article_id, 0) + 1
        used += len(passage.body)
        picked.append(passage)
        if len(picked) >= limit:
            break
    return picked


def retrieve(conn, question: str, scope: Scope, sources) -> Retrieval:
    """sources 是已启用来源，用来从问题里认作者。scope 是页面或接口给的显式范围。"""
    question = (question or "").strip()
    companies = detect_companies(conn, question)
    authors = detect_authors(question, sources)
    effective = scope
    if authors and len(scope.source_ids) > 0:
        narrowed = [source.id for source in sources if source.author in authors and source.id in scope.source_ids]
        if narrowed:
            effective = replace(scope, source_ids=narrowed)
    if scope.article_id is not None:
        passages = _pick(_article_in_order(conn, effective), per_article=40, limit=40, budget=CHAR_BUDGET * 2)
        return Retrieval(passages, companies, authors, effective)

    candidates = _fts(conn, _tsquery_text(conn, question), effective, 80)
    for company in companies[:3]:
        candidates.extend(_alias_hits(conn, company, effective, 30))
    if scope.company_id is not None and not any(c["id"] == scope.company_id for c in companies):
        row = conn.execute(
            "SELECT id, ticker, name_zh, name_en, market, aliases FROM companies WHERE id = %s",
            (scope.company_id,),
        ).fetchone()
        if row:
            candidates.extend(_alias_hits(conn, dict(row), effective, 30))
    if not candidates:
        candidates = _leading(conn, effective, 40)
    if any(word in question for word in RECENT_WORDS):
        head = candidates[:60]
        head.sort(key=lambda item: (item.published_at is not None, item.published_at), reverse=True)
        candidates = head + candidates[60:]
    passages = _pick(candidates, per_article=MAX_PER_ARTICLE, limit=MAX_PASSAGES, budget=CHAR_BUDGET)
    return Retrieval(passages, companies, authors, effective)


_CITE = re.compile(r"\[(\d{1,2})\]")


def cited_numbers(answer: str, count: int) -> list[int]:
    found: list[int] = []
    for match in _CITE.finditer(answer or ""):
        number = int(match.group(1))
        if 1 <= number <= count and number not in found:
            found.append(number)
    return found

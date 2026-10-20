"""检索：关键词检索页、问库和 JSON 接口共用的一套召回与打分。

问题先拆开：公司用别名自动机认（不靠分词，「看拼多多」不会被切成「看拼 / 多多」），作者按名字认，
引号里的是必须出现的短语，减号开头的是排除词。剩下的文字交给 zhparser 切词，按词性去掉代词、
助词、连词、介词这些虚词，再去掉问句套话（怎么看、哪些、观点……）。

每个词按它在全库段落块里出现的块数算 IDF。很常见的词（「公司」「数据」）只参与打分，不拿来召回，
所以库再大，召回也只碰少数段落。

召回走全文索引，几路合并：全部词都命中、任一词命中、问到的公司所在文章里含这些词的段落；
只问一家公司、没有别的词时，按年份分层取这家公司的段落；什么词都没有时取范围内各篇的开头。

打分是 BM25 乘覆盖率（问题里的词组命中得越全越好），再加公司命中深度、标题和相邻词组；
问「最近」时按时间加权，问到某一年时偏向那一年。挑选时每篇封顶几段、低于榜首一定比例的不要；
梳理类问题按年份轮流取，「哪些作者」类按作者轮流取，避免全挤在一两篇里。
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from researchlib.chunks import TRANSLATION_ORD_BASE
from researchlib.mentions import (
    CompanyMatcher,
    company_rules,
    compile_exclude,
    excluded_ranges,
    inside,
    iter_alias_specs,
    load_company_rows,
)
from researchlib.web.queries import Scope, scope_clause

# 问句里常见、对召回没有帮助的词。词性过滤之后剩下的套话在这里去掉。
STOPWORDS = frozenset(
    {
        "什么", "怎么", "怎么样", "怎么看", "如何", "哪些", "哪个", "哪位", "为什么", "是否", "有没有", "能否",
        "看法", "观点", "认为", "看待", "觉得", "判断", "讨论", "讨论过", "提到", "提过", "说过", "讲过", "谈到",
        "谈过", "写过", "最近", "近期", "最新", "目前", "现在", "如今", "这个", "那个", "这些", "那些", "他们",
        "我们", "你们", "一下", "一些", "以及", "还是", "可以", "应该", "关于", "对于", "有关", "相关", "文章",
        "作者", "库里", "资料", "内容", "出处", "梳理", "汇总", "总结", "整合", "介绍", "分析", "问题", "情况",
        "怎样", "多少", "意味着", "还有", "各位", "分别", "主要", "核心", "每条", "条带", "一般", "大家", "公司",
        "这家公司", "今天", "昨天", "一天", "当天", "这周", "本周", "这几天", "这段时间", "这一天",
    }
)
EN_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "what", "how", "why", "about", "does", "did", "are", "was", "is", "of", "to", "in"}
)
# zhparser 词性：保留名词、动词、形容词、成语、简称、习用语、未知词、外文和数字、状态词、区别词、词根、处所词。
KEEP_TYPES = frozenset("nvaijlxezbgs")
RECENT_WORDS = ("最近", "近期", "最新", "目前", "现在", "这周", "本周", "今年", "如今", "这段时间")
TIMELINE_WORDS = (
    "梳理", "变化", "演变", "历史", "时间线", "按时间", "历年", "这些年", "一直", "前后", "脉络", "怎么变",
)
AUTHOR_INTENT = ("哪些作者", "哪位作者", "各位作者", "哪几位", "谁")

K1 = 1.2
B = 0.4
CANDIDATES = 1500
DF_TTL_S = 600.0


@dataclass(frozen=True)
class Budget:
    passages: int
    chars: int
    per_article: int


BUDGETS = {
    # 问库页上马上列出的「相关原文」
    "display": Budget(40, 36000, 2),
    # 交给模型一次读完
    "llm": Budget(80, 64000, 3),
    # 资料太多时分批读（map-reduce）
    "deep": Budget(240, 180000, 2),
    # JSON 接口 level=chunk 的默认份量
    "api": Budget(20, 16000, 3),
}


@dataclass
class Term:
    text: str
    kind: str  # word | company | phrase | expansion
    group: int
    forms: list[str] = field(default_factory=list)
    weight: float = 1.0
    company_id: int | None = None
    df: int = 0
    idf: float = 0.0
    # 够少见，才拿来召回。很常见的词只参与打分。
    selective: bool = True


@dataclass
class Query:
    raw: str
    terms: list[Term]
    companies: list[dict]
    authors: list[str]
    excludes: list[str]
    pairs: list[tuple[str, str]]
    years: list[int]
    recent: bool
    overview: bool
    timeline: bool
    author_intent: bool

    @property
    def content_terms(self) -> list[Term]:
        return [term for term in self.terms if term.kind in ("word", "phrase", "expansion")]

    def highlight_terms(self) -> list[str]:
        found: list[str] = []
        for term in self.terms:
            for form in term.forms or [term.text]:
                if form and form not in found:
                    found.append(form)
        return found


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
    matched: list[str] = field(default_factory=list)
    coverage: float = 0.0
    # 'zh' 是英文文章的中文译文块；anchor 是对照页上那一段的锚点。
    lang: str = ""
    anchor: str = ""

    @property
    def url(self) -> str:
        link = f"/articles/{self.article_id}?chunk={self.ord}"
        return link + (f"#{self.anchor}" if self.anchor else "")


@dataclass
class Retrieval:
    passages: list[Passage]
    companies: list[dict]
    authors: list[str]
    scope: Scope
    terms: list[str] = field(default_factory=list)
    query: Query | None = None
    candidates: int = 0


@dataclass
class ArticleHit:
    article_id: int
    title: str
    source_id: str
    published_at: object
    score: float
    full: bool
    passages: list[Passage]


@dataclass
class SearchResult:
    total: int  # 全部词组都命中的文章数（精确计数）
    partial: int  # 候选里只命中一部分、但相关度够的文章数
    hits: list[ArticleHit]  # 这一页
    terms: list[str]  # 用来高亮的词
    query: Query | None
    counts: dict[str, int]  # 按来源分的全部命中篇数
    available: int = 0  # 能翻到的篇数（全部命中排在前，部分命中在后）


_QUOTE = re.compile(r'"([^"]+)"|“([^”]+)”|「([^」]+)」|『([^』]+)』')
_EXCLUDE = re.compile(r"(?:^|\s)[-－]([^\s]+)")
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_LATIN_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9.+\-]*$")
_CJK = re.compile(r"[㐀-鿿]")
_SPLITTER = "，"


def _is_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


# —— 全库统计：块数、平均长度、各词出现的块数。缓存几分钟，库变了自己会过期。——

_stats_cache: dict[str, tuple[float, float]] = {}
_df_cache: dict[str, tuple[int, float]] = {}
_common_cache: dict[str, tuple[dict[str, int], float]] = {}
# 写法 → 交给全文索引的 tsquery 文本（去掉了切词补出来的子词）。分词参数固定，进程里缓存着就行。
_tsq_cache: dict[str, str] = {}
# 块数在这以下时精确统计（几十毫秒）；更大的库用表的估计行数加抽样的平均长度，不再全表扫一遍。
EXACT_STATS_BELOW = 50000
STATS_SAMPLE_ROWS = 20000


def _corpus_stats(conn) -> tuple[int, float]:
    now = time.monotonic()
    cached = _stats_cache.get("corpus")
    if cached and now - cached[1] < DF_TTL_S:
        total, avgdl = _stats_cache["corpus_values"]
        return int(total), avgdl
    row = conn.execute("SELECT reltuples::bigint AS n FROM pg_class WHERE oid = 'article_chunks'::regclass").fetchone()
    estimate = int(row["n"] or 0) if row else 0
    if estimate < EXACT_STATS_BELOW:
        row = conn.execute(
            "SELECT count(*) AS n, coalesce(avg(length(body)), 500) AS avgdl FROM article_chunks"
        ).fetchone()
        total = int(row["n"])
        avgdl = float(row["avgdl"] or 500)
    else:
        # BM25 只用平均长度做归一化，抽两万行左右估就够了；REPEATABLE 让同一时刻的结果稳定。
        percent = max(0.5, min(100.0, STATS_SAMPLE_ROWS * 100 / estimate))
        row = conn.execute(
            f"SELECT coalesce(avg(length(body)), 500) AS avgdl FROM article_chunks"
            f" TABLESAMPLE SYSTEM ({percent:.3f}) REPEATABLE (7)"
        ).fetchone()
        total = estimate
        avgdl = float(row["avgdl"] or 500)
    _stats_cache["corpus"] = (float(total), now)
    _stats_cache["corpus_values"] = (float(total), avgdl)
    return total, avgdl


def reset_stats_cache() -> None:
    _stats_cache.clear()
    _df_cache.clear()
    _common_cache.clear()


_TSQ_CHAIN = re.compile(r"\s*(<->|<\d+>)\s*")
_TSQ_LEXEME = re.compile(r"^'((?:[^']|'')*)'$")


def _quote_lexeme(word: str) -> str:
    return "'" + word.replace("'", "''") + "'"


def reduce_phrase(tsquery_text: str) -> str:
    """phraseto_tsquery 的结果里，去掉 zhparser 给长词补出来的子词。

    multi_short / seg_with_duality 打开后，「护城河」切成 '护城河' <-> '护城'。索引里两者总是挨着出现，
    所以只查 '护城河' 结果一样，但短语要回表核对位置（逐行解压 tsvector），单个词条全文索引就能直接答。
    只处理「词 <-> 词 <-> …」这种简单的链；词条被相邻的另一个词条包含、又不相同，就去掉，距离照原样累加。
    """
    text = (tsquery_text or "").strip()
    if not text:
        return ""
    parts = _TSQ_CHAIN.split(text)
    lexemes, operators = parts[0::2], parts[1::2]
    words = []
    for lexeme in lexemes:
        match = _TSQ_LEXEME.match(lexeme.strip())
        if match is None:
            return text  # 有 & | ! 或括号，不是简单的链，原样用
        words.append(match.group(1).replace("''", "'"))
    if len(words) == 1:
        return text
    positions = [0]
    for operator in operators:
        positions.append(positions[-1] + (1 if operator == "<->" else int(operator[1:-1])))
    kept: list[tuple[int, str]] = []
    for index, word in enumerate(words):
        neighbours = [words[other] for other in (index - 1, index + 1) if 0 <= other < len(words)]
        if any(word != other and word in other for other in neighbours):
            continue
        kept.append((positions[index], word))
    if not kept:
        return text
    out = _quote_lexeme(kept[0][1])
    previous = kept[0][0]
    for position, word in kept[1:]:
        gap = position - previous
        out += (" <-> " if gap == 1 else f" <{gap}> ") + _quote_lexeme(word)
        previous = position
    return out


def single_lexeme(tsquery_text: str) -> str | None:
    """tsquery 只有一个词条时返回这个词条。"""
    match = _TSQ_LEXEME.match((tsquery_text or "").strip())
    return match.group(1).replace("''", "'") if match else None


def _form_tsquery_sql(forms: list[str], params: dict, prefix: str) -> str:
    parts = []
    for index, form in enumerate(forms):
        key = f"{prefix}_{index}"
        reduced = _tsq_cache.get(form)
        if reduced:
            params[key] = reduced
            parts.append(f"%({key})s::tsquery")
        else:
            params[key] = form
            parts.append(f"phraseto_tsquery('chinese', %({key})s)")
    return "(" + " || ".join(parts) + ")" if parts else ""


def _common_lexemes(conn) -> dict[str, int]:
    """全库最常见的那几百个词条各出现在多少块里，从 ANALYZE 的统计信息里读，不用数。

    常见词正是数起来最慢的（几万块逐个过）；没有统计信息（新库还没 ANALYZE）时返回空，退回去数。
    """
    now = time.monotonic()
    cached = _common_cache.get("chunks")
    if cached and now - cached[1] < DF_TTL_S:
        return cached[0]
    rows = conn.execute(
        """
        SELECT e.elem, e.freq, c.reltuples
        FROM pg_stats s
        JOIN pg_class c ON c.oid = 'article_chunks'::regclass
        CROSS JOIN LATERAL unnest(
          s.most_common_elems::text::text[],
          s.most_common_elem_freqs[1:cardinality(s.most_common_elems::text::text[])]
        ) AS e(elem, freq)
        WHERE s.schemaname = current_schema() AND s.tablename = 'article_chunks' AND s.attname = 'search_tsv'
        """
    ).fetchall()
    found = {
        row["elem"]: int(round(float(row["freq"]) * float(row["reltuples"])))
        for row in rows
        if row["reltuples"] and float(row["reltuples"]) > 0
    }
    _common_cache["chunks"] = (found, now)
    return found


def _document_frequency(conn, term: Term, total: int) -> int:
    key = term.kind + ":" + "|".join(term.forms)
    now = time.monotonic()
    cached = _df_cache.get(key)
    if cached and now - cached[1] < DF_TTL_S:
        return cached[0]
    if len(term.forms) == 1:
        lexeme = single_lexeme(_tsq_cache.get(term.forms[0], ""))
        if lexeme is not None:
            common = _common_lexemes(conn)
            if lexeme in common:
                found = min(max(total, 1), common[lexeme])
                _df_cache[key] = (found, now)
                return found
    params: dict = {}
    tsq = _form_tsquery_sql(term.forms, params, "df")
    # 封顶计数：超过库的四分之一就不再数下去，IDF 已经很低，精确值不影响排序。
    cap = max(20000, total // 4)
    params["cap"] = cap
    row = conn.execute(
        f"SELECT count(*) AS n FROM (SELECT 1 FROM article_chunks WHERE search_tsv @@ {tsq} LIMIT %(cap)s) s",
        params,
    ).fetchone()
    found = int(row["n"])
    _df_cache[key] = (found, now)
    return found


def _usable_forms(conn, forms: list[str]) -> list[str]:
    """去掉切词后为空的写法（纯标点之类），否则 phraseto_tsquery 会得到空查询。顺手记下精简后的 tsquery。"""
    usable = []
    for form in forms:
        cached = _tsq_cache.get(form)
        if cached is not None:
            if cached:
                usable.append(form)
            continue
        row = conn.execute(
            "SELECT numnode(q) AS n, q::text AS t FROM phraseto_tsquery('chinese', %s) AS q", (form,)
        ).fetchone()
        if int(row["n"] or 0) > 0:
            _tsq_cache[form] = reduce_phrase(row["t"])
            usable.append(form)
        else:
            _tsq_cache[form] = ""
    return usable


def _selective_limit(total: int) -> int:
    """出现在多少块以内的词才拿来召回。小库全收；大库按 6% 算，最多六万块，召回成本有上限。"""
    return min(max(3000, int(total * 0.06)), 60000)


def _fill_stats(conn, terms: list[Term]) -> list[Term]:
    total, _avgdl = _corpus_stats(conn)
    kept = []
    for term in terms:
        term.forms = _usable_forms(conn, term.forms or [term.text])
        if not term.forms:
            continue
        df = _document_frequency(conn, term, total)
        term.df = df
        term.idf = math.log(1 + (max(total, 1) - df + 0.5) / (df + 0.5))
        term.selective = df <= _selective_limit(total)
        kept.append(term)
    return kept


# —— 读懂问题 ——


def _segment(conn, text: str) -> list[tuple[str, str]]:
    if not text.strip():
        return []
    rows = conn.execute(
        """
        SELECT p.token, t.alias
        FROM ts_parse('zhparser', %s) p
        JOIN ts_token_type('zhparser') t ON t.tokid = p.tokid
        """,
        (text,),
    ).fetchall()
    return [(row["token"], row["alias"]) for row in rows]


def _content_tokens(conn, text: str) -> list[str]:
    tokens: list[str] = []
    for token, kind in _segment(conn, text):
        token = token.strip()
        if not token or kind not in KEEP_TYPES:
            continue
        if _is_cjk(token):
            if len(token) < 2 or token in STOPWORDS:
                continue
        elif token.isdigit():
            if len(token) < 3 or _YEAR.fullmatch(token):
                continue
        else:
            if not _LATIN_WORD.match(token) or len(token) < 2 or token.lower() in EN_STOPWORDS:
                continue
        if token not in tokens:
            tokens.append(token)
    # multi_short / duality 会在「护城河」之后再给「护城」，只留长的。
    return [t for t in tokens if not any(t != other and t in other for other in tokens)]


def _whitespace_pieces(text: str) -> list[tuple[str, bool]]:
    """按空格切开。返回 (片段, 是否和前一段是「或」的关系)。"""
    pieces: list[tuple[str, bool]] = []
    join_next = False
    for piece in text.split():
        if piece in ("or", "OR", "|", "或"):
            join_next = bool(pieces)
            continue
        pieces.append((piece, join_next))
        join_next = False
    return pieces


def analyze(
    conn,
    question: str,
    sources,
    *,
    company_rows: list[dict] | None = None,
    expansions: dict[str, list[str]] | None = None,
) -> Query:
    """把问题拆成公司、作者、短语、排除词和检索词。expansions 是模型给的同义词：{原词: [同义词]}。"""
    raw = (question or "").strip()[:500]
    text = raw
    phrases: list[str] = []
    for match in _QUOTE.finditer(text):
        phrase = next(group for group in match.groups() if group)
        if phrase.strip() and phrase.strip() not in phrases:
            phrases.append(phrase.strip())
    text = _QUOTE.sub(" ", text)
    excludes = [match.group(1) for match in _EXCLUDE.finditer(text) if match.group(1)]
    text = _EXCLUDE.sub(" ", text)
    years = sorted({int(year) for year in _YEAR.findall(text)})

    rows = company_rows if company_rows is not None else load_company_rows(conn)
    matcher = cached_matcher("aho", rows) if rows else None
    by_id = {int(row["id"]): row for row in rows}
    author_names = sorted({source.author for source in sources if source.author}, key=len, reverse=True)

    terms: list[Term] = []
    companies: list[dict] = []
    authors: list[str] = []
    pairs: list[tuple[str, str]] = []
    for index, phrase in enumerate(phrases):
        terms.append(Term(phrase, "phrase", index, forms=[phrase], weight=1.3))
    # 每个词自成一组（组与组之间是「且」）；用 or 连起来的片段并成一组（组内是「或」）。
    next_group = len(phrases)
    piece_group = -1
    for piece, joined in _whitespace_pieces(text):
        shared = piece_group if joined and piece_group >= 0 else None
        masked = piece
        found: list[tuple[str, str, list[str], int | None]] = []  # (文字, 类型, 写法, 公司 id)
        if matcher is not None:
            spans = sorted(matcher.spans(piece), key=lambda span: span[0])
            for _start, _end, company_id in spans:
                row = by_id[company_id]
                if all(int(c["id"]) != company_id for c in companies):
                    companies.append(dict(row))
                if all(t.company_id != company_id for t in terms) and all(f[3] != company_id for f in found):
                    found.append((row["name_zh"], "company", [a for a, _fold in iter_alias_specs(row)], company_id))
            for start, end, _company_id in sorted(spans, key=lambda span: span[0], reverse=True):
                masked = masked[:start] + _SPLITTER + masked[end:]
        for name in author_names:
            if name in masked:
                if name not in authors:
                    authors.append(name)
                masked = masked.replace(name, _SPLITTER)
        tokens = _content_tokens(conn, masked)
        for token in tokens:
            if not any(t.text == token and t.kind == "word" for t in terms):
                found.append((token, "word", [token], None))
        for first, second in zip(tokens, tokens[1:], strict=False):
            if first + second in masked:
                pairs.append((first, second))
        for text_, kind, forms, company_id in found:
            if shared is not None:
                group = shared
            else:
                group = next_group
                next_group += 1
            weight = 1.5 if kind == "company" else 1.0
            terms.append(Term(text_, kind, group, forms=forms, weight=weight, company_id=company_id))
        if found:
            piece_group = shared if shared is not None else next_group - len(found)
    if expansions:
        next_group = max([t.group for t in terms] + [-1]) + 1
        for original, alternatives in expansions.items():
            base = next((t for t in terms if t.text == original), None)
            for alternative in alternatives[:4]:
                alternative = (alternative or "").strip()
                if not alternative or len(alternative) > 20 or any(alternative in (t.forms or [t.text]) for t in terms):
                    continue
                if base is not None:
                    terms.append(Term(alternative, "expansion", base.group, forms=[alternative], weight=0.7))
                else:
                    # 模型补充的新词：单独成组，只加分，不计入覆盖率。
                    terms.append(Term(alternative, "expansion", -1 - next_group, forms=[alternative], weight=0.4))
                    next_group += 1

    terms = _fill_stats(conn, terms)
    # 问题很长时只留信息量最大的十几个词，避免把整句话都拿去召回。
    originals = [t for t in terms if t.kind != "expansion"]
    if len(originals) > 12:
        keep = {id(t) for t in sorted(originals, key=lambda t: t.idf, reverse=True)[:12]}
        terms = [t for t in terms if t.kind == "expansion" or id(t) in keep]
    content = [t for t in terms if t.kind in ("word", "phrase")]
    recent = any(word in raw for word in RECENT_WORDS)
    timeline = any(word in raw for word in TIMELINE_WORDS)
    author_intent = any(word in raw for word in AUTHOR_INTENT)
    return Query(
        raw=raw,
        terms=terms,
        companies=companies,
        authors=authors,
        excludes=excludes,
        pairs=pairs,
        years=years,
        recent=recent,
        overview=not content,
        timeline=timeline,
        author_intent=author_intent,
    )


def preset_query(query: Query) -> Query:
    """预设问题（公司页、作者页的「问 AI」）没有检索词，只看范围。"""
    return replace(query, terms=[t for t in query.terms if t.kind == "company"], pairs=[], overview=True, timeline=True)


# —— 召回 ——


# 译文块的标题用中文标题（有的话），打分时的标题上下文和页面上显示的都是中文。
_SELECT_COLUMNS = """
c.article_id, c.ord, c.heading, c.body, c.lang, c.anchor,
CASE WHEN c.lang = 'zh' THEN coalesce(
  nullif((SELECT tr.title_zh FROM article_translations tr WHERE tr.article_id = c.article_id), ''), a.title
) ELSE a.title END AS title,
a.source_id, a.published_at
"""


def _article_match(tsq: str) -> str:
    """文章级的全文命中：原文的标题和正文，加上中文译文的标题和正文。

    写成两个子查询的并集，两边都能走各自的 GIN 索引；写成 OR EXISTS 会退化成逐行过滤，库大了很慢。
    """
    return (
        f"a.id IN (SELECT ai.id FROM articles ai WHERE ai.search_tsv @@ {tsq}"
        f" UNION ALL SELECT tq.article_id FROM article_translations tq WHERE tq.search_tsv @@ {tsq})"
    )


def _prefer_translation(rows: list[dict]) -> list[dict]:
    """同一篇英文文章的原文块和中文译文块都召回时，只留译文块，免得同一段内容占两份名额。"""
    translated = {row["article_id"] for row in rows if row.get("lang") == "zh"}
    if not translated:
        return rows
    return [row for row in rows if row.get("lang") == "zh" or row["article_id"] not in translated]


def _group_tsquery(query: Query, params: dict, *, only_selective: bool, operator: str, prefix: str) -> str:
    by_group: dict[int, list[Term]] = defaultdict(list)
    for term in query.terms:
        if term.group < 0:
            continue
        by_group[term.group].append(term)
    parts = []
    for group, terms in sorted(by_group.items()):
        if only_selective and not any(term.selective for term in terms):
            continue
        forms = [form for term in terms for form in term.forms]
        sql = _form_tsquery_sql(forms, params, f"{prefix}{group}")
        if sql:
            parts.append(sql)
    if not parts:
        return ""
    return "(" + f" {operator} ".join(parts) + ")"


# 一路召回最多拿这么多索引命中来排名。常见词在大库里可能命中几十万块，不封顶就会拖慢。
FTS_POOL = 20000


def _fts_keys(conn, scope: Scope, tsq: str, params: dict, limit: int, *, company_ids: list[int] | None = None):
    """一路全文召回，只取 (文章, 块号)。正文等各路合并去重后再一次取回。"""
    where, scope_params = scope_clause(scope)
    extra = ""
    if company_ids:
        params["fts_companies"] = company_ids
        extra = (
            " AND EXISTS (SELECT 1 FROM article_mentions fm WHERE fm.article_id = a.id"
            " AND fm.company_id = ANY(%(fts_companies)s))"
        )
    params.update(scope_params)
    params["fts_limit"] = limit
    params["fts_pool"] = FTS_POOL
    rows = conn.execute(
        f"""
        SELECT pool.article_id, pool.ord
        FROM (
          SELECT c.article_id, c.ord, c.search_tsv, a.published_at
          FROM article_chunks c
          JOIN articles a ON a.id = c.article_id
          WHERE {where} AND c.search_tsv @@ {tsq}{extra}
          LIMIT %(fts_pool)s
        ) pool
        ORDER BY ts_rank_cd(pool.search_tsv, {tsq}) DESC, pool.published_at DESC NULLS LAST
        LIMIT %(fts_limit)s
        """,
        params,
    ).fetchall()
    return [(row["article_id"], row["ord"]) for row in rows]


def _title_keys(conn, query: Query, scope: Scope, limit: int) -> list[tuple[int, int]]:
    """标题命中的文章。段落块不含标题，只按块召回会漏掉「标题写了、正文没写」的文章，这里取它们的第一块。

    文章级全部命中的都算（顺带接住词散在不同段落里的文章），标题里就有全部词组的排前面，其余按时间新的在前。
    标题单独有索引（title_tsv），判断标题命中很便宜；以前按整篇的 ts_rank 排，常见词能命中几万篇，
    逐篇解压整篇的 tsvector，库大了很慢。
    """
    params: dict = {}
    words = Query(**{**query.__dict__, "terms": [t for t in query.terms if t.kind != "company"]})
    tsq = _group_tsquery(words, params, only_selective=False, operator="&&", prefix="title") if words.terms else ""
    company_ids = [t.company_id for t in query.terms if t.kind == "company" and t.company_id is not None]
    where, scope_params = scope_clause(scope)
    params.update(scope_params)
    params["title_limit"] = limit
    conditions = [where]
    for index, company_id in enumerate(company_ids):
        params[f"title_company_{index}"] = company_id
        conditions.append(
            "EXISTS (SELECT 1 FROM article_mentions tm WHERE tm.article_id = a.id"
            f" AND tm.company_id = %(title_company_{index})s AND tm.depth = 'title')"
        )
    if tsq:
        conditions.append(_article_match(tsq))
        title_hit = (
            f"(a.title_tsv @@ {tsq} OR a.id IN"
            f" (SELECT tt.article_id FROM article_translations tt WHERE tt.title_tsv @@ {tsq}))"
        )
        order = f"{title_hit} DESC, a.published_at DESC NULLS LAST, a.id DESC"
    elif company_ids:
        order = "a.published_at DESC NULLS LAST"
    else:
        return []
    params["zh_first"] = TRANSLATION_ORD_BASE
    rows = conn.execute(
        f"""
        SELECT a.id,
               CASE WHEN EXISTS (
                 SELECT 1 FROM article_chunks zc WHERE zc.article_id = a.id AND zc.ord = %(zh_first)s
               ) THEN %(zh_first)s ELSE 0 END AS first_ord
        FROM articles a
        WHERE {' AND '.join(conditions)}
        ORDER BY {order}
        LIMIT %(title_limit)s
        """,
        params,
    ).fetchall()
    # 有中文译文的英文文章取译文的第一块。
    return [(row["id"], row["first_ord"]) for row in rows]


def _rows_for_keys(conn, keys: list[tuple[int, int]]) -> list[dict]:
    if not keys:
        return []
    return conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM unnest(%s::bigint[], %s::int[]) AS k(article_id, ord)
        JOIN article_chunks c ON c.article_id = k.article_id AND c.ord = k.ord
        JOIN articles a ON a.id = c.article_id
        """,
        ([key[0] for key in keys], [key[1] for key in keys]),
    ).fetchall()


def _company_rows(conn, company: dict, scope: Scope, per_year: int):
    """只问公司时的召回：这家公司真正被谈到的段落，按年份分层，深入提及优先。

    先只看深入提及（标题、全文、单节）的文章；不够一百块再把一带而过的也算上。
    段落里有没有这家公司走全文索引预筛，打分时再用别名逐字核对。
    """
    forms = [text for text, _ in iter_alias_specs(company)]
    params: dict = {}
    tsq = _form_tsquery_sql(_usable_forms(conn, forms), params, "co")
    if not tsq:
        return []
    where, scope_params = scope_clause(replace(scope, company_id=None))
    params.update(scope_params)
    params.update({"company": int(company["id"]), "per_year": per_year})
    found: list[dict] = []
    for depth_filter in ("AND m.depth <> 'passing'", ""):
        found = conn.execute(
            f"""
            SELECT * FROM (
              SELECT {_SELECT_COLUMNS}, m.depth,
                     row_number() OVER (
                       PARTITION BY date_part('year', a.published_at AT TIME ZONE 'Asia/Shanghai')
                       ORDER BY (m.depth = 'passing'), m.hit_count DESC, a.published_at DESC, c.ord
                     ) AS rn
              FROM article_mentions m
              JOIN articles a ON a.id = m.article_id
              JOIN article_chunks c ON c.article_id = a.id
              WHERE m.company_id = %(company)s {depth_filter} AND {where} AND c.search_tsv @@ {tsq}
            ) ranked
            WHERE rn <= %(per_year)s
            """,
            params,
        ).fetchall()
        if len(found) >= 100:
            break
    return found


def _leading_rows(conn, scope: Scope, limit: int):
    """什么检索词都没有时（作者页预设、「汇总这一天」），取范围内各篇开头两块，新的在前。"""
    where, params = scope_clause(scope)
    params["lead_limit"] = limit
    params["zh_first"] = TRANSLATION_ORD_BASE
    return conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        WHERE {where} AND (c.ord < 2 OR (c.ord >= %(zh_first)s AND c.ord < %(zh_first)s + 2))
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC, c.ord
        LIMIT %(lead_limit)s
        """,
        params,
    ).fetchall()


def _article_rows(conn, scope: Scope):
    """只问这一篇：按原文顺序整篇给。英文文章有译文时给译文。"""
    where, params = scope_clause(scope)
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        WHERE {where}
        ORDER BY c.ord
        LIMIT 800
        """,
        params,
    ).fetchall()
    return _prefer_translation(list(rows))[:400]


def _candidates(conn, query: Query, scope: Scope, limit: int = CANDIDATES, *, enough: int | None = None) -> list[dict]:
    """几路召回合并。enough：前两路已经召回这么多块时，不再用「任一少见词」撒网。"""
    found: dict[tuple[int, int], dict] = {}
    keys: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def add_keys(items):
        for key in items:
            if key not in seen:
                seen.add(key)
                keys.append(key)

    def add_rows(rows):
        for row in rows:
            key = (row["article_id"], row["ord"])
            if key not in found:
                found[key] = dict(row)
                seen.add(key)

    groups = {t.group for t in query.terms if t.group >= 0}
    word_terms = [t for t in query.terms if t.kind in ("word", "phrase", "expansion")]
    company_ids = [t.company_id for t in query.terms if t.kind == "company" and t.company_id is not None]
    if scope.company_id is not None and scope.company_id not in company_ids:
        company_ids.append(scope.company_id)
    enough = enough if enough is not None else limit

    if query.terms:
        # 1) 全部词组都命中：最相关的一批，单独保证进候选。
        if len(groups) >= 2:
            params: dict = {}
            tsq = _group_tsquery(query, params, only_selective=False, operator="&&", prefix="and")
            if tsq:
                add_keys(_fts_keys(conn, scope, tsq, params, limit))
        # 2) 问到了公司又有别的词：只在提到这家公司的文章里找含这些词的段落。
        if company_ids and word_terms:
            params = {}
            forms = [form for term in word_terms for form in term.forms]
            tsq = _form_tsquery_sql(forms, params, "cw")
            if tsq:
                add_keys(_fts_keys(conn, scope, tsq, params, limit, company_ids=company_ids))
        # 3) 任一少见词命中。很常见的词不在这里召回，只参与打分。前两路已经够多就不再撒网。
        # 3) 标题命中的文章：段落里不一定再写一遍，按文章取第一块。
        add_keys(_title_keys(conn, query, scope, min(limit, 300)))
        # 4) 任一少见词命中。很常见的词不在这里召回，只参与打分。前几路已经够多就不再撒网。
        if len(keys) < enough:
            params = {}
            tsq = _group_tsquery(query, params, only_selective=True, operator="||", prefix="or")
            if not tsq and len(keys) < 200:
                # 全是常见词、全部命中的又不多：退回用最少见的那个词，至少能召回。
                rare = sorted(query.terms, key=lambda t: t.df)[:1]
                forms = [form for term in rare for form in term.forms]
                tsq = _form_tsquery_sql(forms, params, "rare")
            if tsq:
                add_keys(_fts_keys(conn, scope, tsq, params, limit))
        add_rows(_rows_for_keys(conn, keys))
    if not word_terms:
        companies = [t for t in query.terms if t.kind == "company"]
        targets = [{"id": t.company_id, **_company_lookup(query, t.company_id)} for t in companies]
        if scope.company_id is not None and all(t["id"] != scope.company_id for t in targets):
            row = conn.execute(
                "SELECT id, ticker, name_zh, name_en, market, aliases, alias_rules FROM companies WHERE id = %s",
                (scope.company_id,),
            ).fetchone()
            if row:
                targets.append(dict(row))
        for company in targets[:3]:
            add_rows(_company_rows(conn, company, scope, per_year=60))
        if targets:
            title_query = replace(query, terms=[t for t in query.terms if t.kind == "company"])
            title_keys = [key for key in _title_keys(conn, title_query, scope, 200) if key not in seen]
            add_rows(_rows_for_keys(conn, title_keys))
        if not targets:
            add_rows(_leading_rows(conn, scope, 600))
    return _prefer_translation(list(found.values()))


def _company_lookup(query: Query, company_id: int | None) -> dict:
    for company in query.companies:
        if int(company["id"]) == company_id:
            return company
    return {}


# —— 打分 ——


class FocusMatcher:
    """只数问到的那几家公司。打分要对几千个候选段落计数，逐字走自动机太慢，这里用预编译正则。

    规则和 CompanyMatcher 一致：中文按字面、同一位置取最长；英文按词边界，短写法区分大小写。
    """

    def __init__(self, companies: list[dict]):
        self._lookup: dict[str, int] = {}
        chinese: list[str] = []
        folded: list[str] = []
        sensitive: list[str] = []
        # 和 CompanyMatcher 同一套排除写法：「烟台苹果」「中美的」里的命中不算。
        self._exclude = {
            int(company["id"]): pattern
            for company in companies
            if (pattern := compile_exclude(company_rules(company).get("exclude") or [])) is not None
        }
        for company in companies:
            for text, casefold in iter_alias_specs(company):
                if _is_cjk(text):
                    chinese.append(text)
                    self._lookup[text] = int(company["id"])
                elif casefold:
                    folded.append(text)
                    self._lookup[text.casefold()] = int(company["id"])
                else:
                    sensitive.append(text)
                    self._lookup[text] = int(company["id"])
        self._patterns: list[tuple[re.Pattern[str], bool]] = []
        if chinese:
            ordered = sorted(set(chinese), key=len, reverse=True)
            self._patterns.append((re.compile("|".join(re.escape(t) for t in ordered)), False))
        for items, casefold in ((folded, True), (sensitive, False)):
            if items:
                ordered = sorted(set(items), key=len, reverse=True)
                body = "|".join(re.escape(t) for t in ordered)
                flags = re.IGNORECASE if casefold else 0
                pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + body + r")(?![A-Za-z0-9])", flags)
                self._patterns.append((pattern, casefold))

    def count(self, text: str) -> dict[int, int]:
        counts: dict[int, int] = {}
        ranges: dict[int, list[tuple[int, int]]] = {}
        for pattern, casefold in self._patterns:
            for match in pattern.finditer(text):
                token = match.group(0)
                company_id = self._lookup.get(token.casefold() if casefold else token)
                if company_id is None:
                    continue
                exclude = self._exclude.get(company_id)
                if exclude is not None:
                    if company_id not in ranges:
                        ranges[company_id] = excluded_ranges(exclude, text)
                    if inside(ranges[company_id], match.start(), match.end()):
                        continue
                counts[company_id] = counts.get(company_id, 0) + 1
        return counts


_matcher_cache: dict[tuple, object] = {}


def _rows_key(rows: list[dict]) -> tuple:
    return tuple(
        (
            int(r["id"]),
            r.get("name_zh"),
            r.get("name_en"),
            r.get("ticker"),
            tuple(r.get("aliases") or ()),
            json.dumps(company_rules(r), ensure_ascii=False, sort_keys=True),
        )
        for r in rows
    )


def cached_matcher(kind: str, rows: list[dict]):
    """公司表不常变，匹配器按公司表内容缓存，不必每次检索都重建。"""
    key = (kind, _rows_key(rows))
    found = _matcher_cache.get(key)
    if found is None:
        if len(_matcher_cache) > 64:
            _matcher_cache.clear()
        found = CompanyMatcher(rows) if kind == "aho" else FocusMatcher(rows)
        _matcher_cache[key] = found
    return found


def _form_counter(form: str):
    """返回一个计数函数。中文按字面数，英文按词边界、不分大小写。"""
    if _is_cjk(form):
        return lambda text, _lowered: text.count(form)
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(form.lower()) + r"(?![A-Za-z0-9])")
    return lambda _text, lowered: len(pattern.findall(lowered))


def _count(text: str, lowered: str, form: str) -> int:
    if not form:
        return 0
    if _is_cjk(form):
        return text.count(form)
    pattern = r"(?<![A-Za-z0-9])" + re.escape(form.lower()) + r"(?![A-Za-z0-9])"
    return len(re.findall(pattern, lowered))


def _age_days(published) -> float | None:
    if published is None:
        return None
    if isinstance(published, datetime):
        moment = published if published.tzinfo else published.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - moment).total_seconds() / 86400)
    return None


def _year_of(published) -> int | None:
    if isinstance(published, datetime):
        return published.year
    return None


def score_rows(conn, query: Query, rows: list[dict], *, scope: Scope, company_rows: list[dict] | None = None):
    _total, avgdl = _corpus_stats(conn)
    company_ids = [t.company_id for t in query.terms if t.kind == "company" and t.company_id is not None]
    focus_ids = list(company_ids)
    if scope.company_id is not None and scope.company_id not in focus_ids:
        focus_ids.append(scope.company_id)
    matcher = None
    # 「苹果」这类要佐证的公司：文章没被记为提到它（比如通篇在讲烟台苹果），段落里的字面命中也不算。
    guarded: set[int] = set()
    if focus_ids:
        rows_for_matcher = company_rows if company_rows is not None else load_company_rows(conn)
        focus_rows = [row for row in rows_for_matcher if int(row["id"]) in focus_ids]
        matcher = cached_matcher("focus", focus_rows)
        guarded = {int(row["id"]) for row in focus_rows if company_rules(row).get("needs")}
    # 只有作者或日期范围时：这些文章深入谈了哪些公司，段落里出现这些名字的更有信息量。
    deep_names: dict[int, list[str]] = {}
    if query.overview and not focus_ids and rows:
        for item in conn.execute(
            """
            SELECT m.article_id, c.name_zh, c.name_en, c.aliases
            FROM article_mentions m
            JOIN companies c ON c.id = m.company_id
            WHERE m.article_id = ANY(%s) AND m.depth <> 'passing'
            """,
            (sorted({row["article_id"] for row in rows}),),
        ).fetchall():
            names = [item["name_zh"], item["name_en"], *(item["aliases"] or [])]
            deep_names.setdefault(item["article_id"], []).extend(n for n in names if n and len(n) >= 2)
    counters = {
        id(term): [(form, _form_counter(form)) for form in term.forms if form]
        for term in query.terms
        if term.kind != "company"
    }
    depths: dict[tuple[int, int], str] = {}
    if focus_ids and rows:
        article_ids = sorted({row["article_id"] for row in rows})
        for item in conn.execute(
            "SELECT article_id, company_id, depth FROM article_mentions"
            " WHERE article_id = ANY(%s) AND company_id = ANY(%s)",
            (article_ids, focus_ids),
        ).fetchall():
            depths[(item["article_id"], item["company_id"])] = item["depth"]

    group_weight: dict[int, float] = {}
    for term in query.terms:
        if term.group < 0 or term.kind == "expansion":
            continue
        group_weight[term.group] = max(group_weight.get(term.group, 0.0), term.idf * term.weight)
    total_weight = sum(group_weight.values())
    phrases = [t for t in query.terms if t.kind == "phrase"]

    passages: list[Passage] = []
    for row in rows:
        heading = row.get("heading") or ""
        body = row.get("body") or ""
        text = heading + "\n" + body
        lowered = text.lower()
        if any(_count(text, lowered, word) for word in query.excludes):
            continue
        if any(counters[id(phrase)][0][1](text, lowered) == 0 for phrase in phrases):
            continue
        dl = max(len(text), 1)
        norm = K1 * (1 - B + B * dl / max(avgdl, 1))
        company_hits = matcher.count(text) if matcher is not None else {}
        title = row.get("title") or ""
        title_lowered = title.lower()
        title_companies = matcher.count(title) if matcher is not None and title else {}
        for company_id in guarded:
            if (row["article_id"], company_id) not in depths:
                company_hits.pop(company_id, None)
                title_companies.pop(company_id, None)
        score = 0.0
        matched_groups: set[int] = set()
        title_groups: set[int] = set()  # 只在标题里出现、这段正文没写的词组，覆盖率只算一半
        matched: list[str] = []
        for term in query.terms:
            if term.kind == "company":
                tf = company_hits.get(term.company_id, 0)
                title_tf = title_companies.get(term.company_id, 0)
                found_forms = [form for form in term.forms if form and (form in text or form.lower() in lowered)]
            else:
                found_forms = []
                tf = 0
                title_tf = 0
                for form, counter in counters[id(term)]:
                    n = counter(text, lowered)
                    if n:
                        tf += n
                        found_forms.append(form)
                    title_tf += counter(title, title_lowered) if title else 0
            # 标题写到的词，算进这篇每一段的上下文：标题是「腾讯专文」，正文不必句句写腾讯。
            effective_tf = tf + 0.5 * min(title_tf, 2)
            if effective_tf <= 0:
                continue
            score += term.weight * term.idf * effective_tf * (K1 + 1) / (effective_tf + norm)
            if title_tf:
                score += 0.3 * term.idf * term.weight
            if term.group >= 0:
                if tf > 0:
                    matched_groups.add(term.group)
                else:
                    title_groups.add(term.group)
            matched.extend(found_forms)
        if total_weight > 0:
            own = sum(group_weight[g] for g in matched_groups if g in group_weight)
            from_title = sum(group_weight[g] for g in title_groups - matched_groups if g in group_weight)
            coverage = (own + 0.5 * from_title) / total_weight
        else:
            coverage = 1.0
        for first, second in query.pairs:
            if first + second in text:
                idf_a = next((t.idf for t in query.terms if t.text == first), 0.0)
                idf_b = next((t.idf for t in query.terms if t.text == second), 0.0)
                score += 0.5 * (idf_a + idf_b)
        score *= 0.1 + 0.9 * coverage**2

        # 公司：段落里确实谈到了这家公司才算；整篇深入谈的文章再加一点。
        if focus_ids:
            hits = sum(company_hits.get(cid, 0) for cid in focus_ids)
            if hits == 0 and row["ord"] == 0 and any(title_companies.get(cid) for cid in focus_ids):
                hits = 1
            if query.overview:
                if hits == 0:
                    continue
                best = min(
                    (_DEPTH_RANK.get(depths.get((row["article_id"], cid), "passing"), 3) for cid in focus_ids),
                    default=3,
                )
                score = math.log1p(hits) * (1.6, 1.4, 1.15, 0.5)[best]
                coverage = 1.0
            elif hits == 0 and scope.company_id is not None:
                score *= 0.3
            if hits:
                best = min(
                    (_DEPTH_RANK.get(depths.get((row["article_id"], cid), "passing"), 3) for cid in focus_ids),
                    default=3,
                )
                score *= (1.15, 1.1, 1.05, 0.7)[best]
        elif query.overview:
            # 只有作者或日期范围：开头那块最能代表全文；谈到具体公司的段落比套话更有信息量。
            score = 1.0 / (1 + row["ord"])
            if any(name in text for name in deep_names.get(row["article_id"], ())):
                score *= 1.8
            coverage = 1.0
        if score <= 0:
            continue
        age = _age_days(row.get("published_at"))
        if query.recent and age is not None:
            score *= 0.55 + 0.9 * math.exp(-age / 400)
        if query.years:
            year = _year_of(row.get("published_at"))
            if year is not None:
                score *= 1.6 if year in query.years else 0.6
        passages.append(
            Passage(
                article_id=row["article_id"],
                ord=row["ord"],
                heading=heading,
                body=body,
                title=title,
                source_id=row["source_id"],
                published_at=row.get("published_at"),
                score=score,
                matched=list(dict.fromkeys(matched)),
                coverage=coverage,
                lang=row.get("lang") or "",
                anchor=row.get("anchor") or "",
            )
        )
    return passages


_DEPTH_RANK = {"title": 0, "whole": 1, "section": 2, "passing": 3}


def _passage_from_row(row: dict) -> Passage:
    return Passage(
        article_id=row["article_id"],
        ord=row["ord"],
        heading=row["heading"],
        body=row["body"],
        title=row["title"],
        source_id=row["source_id"],
        published_at=row["published_at"],
        lang=row.get("lang") or "",
        anchor=row.get("anchor") or "",
    )


# —— 挑选 ——


def _bucket_key(passage: Passage, query: Query, sources_by_id: dict) -> object:
    if query.author_intent:
        source = sources_by_id.get(passage.source_id)
        return source.author if source else passage.source_id
    return _year_of(passage.published_at) or 0


def select(passages: list[Passage], query: Query, budget: Budget, sources_by_id: dict | None = None) -> list[Passage]:
    ranked = sorted(passages, key=lambda p: (p.score, p.published_at is not None, p.published_at), reverse=True)
    if not ranked:
        return []
    if not query.overview:
        floor = ranked[0].score * 0.12
        ranked = [p for p in ranked if p.score >= floor]
        # 全部词组都命中的段落已经不少时，只命中一半以下的就不要了，免得用噪音填满名额。
        full = sum(1 for p in ranked if p.coverage >= 0.99)
        if full >= max(10, budget.passages // 4):
            ranked = [p for p in ranked if p.coverage >= 0.5]
    # 梳理类按年份摊开，「哪些作者」按作者摊开；问「最近」时不摊年份，免得把旧文章顶上来。
    spread = query.author_intent or ((query.overview or query.timeline) and not query.recent)
    if spread:
        buckets: dict[object, list[Passage]] = defaultdict(list)
        for passage in ranked:
            buckets[_bucket_key(passage, query, sources_by_id or {})].append(passage)
        order = sorted(buckets, key=lambda key: buckets[key][0].score, reverse=True)
        interleaved: list[Passage] = []
        while any(buckets[key] for key in order):
            for key in order:
                if buckets[key]:
                    interleaved.append(buckets[key].pop(0))
        ranked = interleaved
    picked: list[Passage] = []
    per: dict[int, int] = {}
    seen_text: set[str] = set()
    used = 0
    for passage in ranked:
        if per.get(passage.article_id, 0) >= budget.per_article:
            continue
        if picked and used + len(passage.body) > budget.chars:
            continue
        # 同一段话出现在两篇里（整段引用、没认出来的转发）只给一次，不占两个名额。
        key = _text_key(passage.body)
        if key and key in seen_text:
            continue
        if key:
            seen_text.add(key)
        per[passage.article_id] = per.get(passage.article_id, 0) + 1
        used += len(passage.body)
        picked.append(passage)
        if len(picked) >= budget.passages:
            break
    return picked


_TEXT_KEY = re.compile(r"[0-9A-Za-z一-鿿]+")


def _text_key(body: str) -> str:
    text = "".join(_TEXT_KEY.findall(body or "")).lower()
    return text if len(text) >= 30 else ""


def retrieve(
    conn,
    question: str,
    scope: Scope,
    sources,
    *,
    purpose: str = "llm",
    preset: bool = False,
    expansions: dict[str, list[str]] | None = None,
    limit: int | None = None,
) -> Retrieval:
    """sources 是已启用来源，用来从问题里认作者。scope 是页面或接口给的显式范围。"""
    budget = BUDGETS.get(purpose, BUDGETS["llm"])
    if limit:
        budget = Budget(limit, max(budget.chars, limit * 900), budget.per_article)
    company_rows = load_company_rows(conn)
    query = analyze(conn, question, sources, company_rows=company_rows, expansions=expansions)
    if preset:
        query = preset_query(query)
    else:
        # 问库是自然语言：库里一次都没出现过的词（「在聊」之类）对召回没用，还会让覆盖率永远凑不满。
        kept = [t for t in query.terms if t.kind != "word" or t.df > 0]
        if len(kept) != len(query.terms):
            content = [t for t in kept if t.kind in ("word", "phrase")]
            query = replace(query, terms=kept, overview=not content)
    effective = scope
    if query.authors and scope.source_ids:
        narrowed = [s.id for s in sources if s.author in query.authors and s.id in scope.source_ids]
        if narrowed:
            effective = replace(scope, source_ids=narrowed)
    sources_by_id = {source.id: source for source in sources}
    if scope.article_id is not None:
        # 只问这一篇：按原文顺序整篇给，太长时截到预算。
        rows = _article_rows(conn, effective)
        picked: list[Passage] = []
        used = 0
        for r in rows:
            if picked and (len(picked) >= 40 or used + len(r["body"]) > CHAR_BUDGET_ARTICLE):
                break
            used += len(r["body"])
            picked.append(_passage_from_row(r))
        return Retrieval(picked, query.companies, query.authors, effective, query.highlight_terms(), query, len(rows))
    per_channel = min(CANDIDATES, max(400, budget.passages * 8))
    rows = _candidates(conn, query, effective, limit=per_channel, enough=budget.passages * 6)
    scored = score_rows(conn, query, rows, scope=effective, company_rows=company_rows)
    picked = select(scored, query, budget, sources_by_id)
    narrowed = effective.date_from or effective.date_to or effective.company_id is not None or query.authors
    if not picked and narrowed and not query.overview:
        # 范围已经很窄（某天、某位作者、某家公司）但词没召回到：退回按范围取，像「汇总这一天」。
        fallback = preset_query(query)
        rows = _candidates(conn, fallback, effective, limit=per_channel, enough=budget.passages * 6)
        scored = score_rows(conn, fallback, rows, scope=effective, company_rows=company_rows)
        picked = select(scored, fallback, budget, sources_by_id)
        query = fallback
    return Retrieval(picked, query.companies, query.authors, effective, query.highlight_terms(), query, len(rows))


CHAR_BUDGET_ARTICLE = 60000


# —— 检索页：按文章聚合 ——


def _full_match_tsquery(query: Query, params: dict) -> str:
    return _group_tsquery(query, params, only_selective=False, operator="&&", prefix="full")


def search_articles(conn, text: str, scope: Scope, sources, *, limit: int = 20, offset: int = 0) -> SearchResult:
    """关键词检索：全部词都命中的文章排前面，其后是只命中一部分的。每篇带最相关的两段原文。"""
    company_rows = load_company_rows(conn)
    query = analyze(conn, text, sources, company_rows=company_rows)
    effective = scope
    if query.authors and scope.source_ids:
        narrowed = [s.id for s in sources if s.author in query.authors and s.id in scope.source_ids]
        if narrowed:
            effective = replace(scope, source_ids=narrowed)
    if not query.terms:
        if query.authors:
            return _author_listing(conn, query, effective, limit, offset)
        return SearchResult(0, 0, [], [], query, {})
    rows = _candidates(conn, query, effective, limit=2000, enough=2000)
    scored = score_rows(conn, query, rows, scope=effective, company_rows=company_rows)
    by_article: dict[int, list[Passage]] = defaultdict(list)
    for passage in scored:
        by_article[passage.article_id].append(passage)
    groups = {t.group for t in query.terms if t.group >= 0}
    titles = {p.article_id: p.title for p in scored}
    hits: list[ArticleHit] = []
    for article_id, items in by_article.items():
        items.sort(key=lambda p: p.score, reverse=True)
        title = titles.get(article_id, "")
        matched_groups = set()
        for term in query.terms:
            if term.group < 0:
                continue
            if any(form in p.matched for p in items for form in term.forms):
                matched_groups.add(term.group)
            elif any(form and (form in title or form.lower() in title.lower()) for form in term.forms):
                matched_groups.add(term.group)
        full = groups <= matched_groups
        score = items[0].score + 0.3 * sum(p.score for p in items[1:3])
        first = items[0]
        hits.append(ArticleHit(article_id, title, first.source_id, first.published_at, score, full, items[:2]))
    full_hits = sorted((h for h in hits if h.full), key=lambda h: h.score, reverse=True)
    partial_hits = sorted((h for h in hits if not h.full), key=lambda h: h.score, reverse=True)
    if partial_hits:
        best = partial_hits[0].score
        partial_hits = [h for h in partial_hits if h.score >= best * 0.2]
    counts = full_match_counts(conn, query, effective)
    total = max(sum(counts.values()), len(full_hits))
    ordered = full_hits + partial_hits
    return SearchResult(
        total=total,
        partial=len(partial_hits),
        hits=ordered[offset : offset + limit],
        terms=query.highlight_terms(),
        query=query,
        counts=counts,
        available=len(ordered),
    )


def full_match_counts(conn, query: Query, scope: Scope) -> dict[str, int]:
    """全部词组都命中的文章数，按来源分。用文章级全文索引，库再大也只是一次索引交集。"""
    params: dict = {}
    word_groups = Query(**{**query.__dict__, "terms": [t for t in query.terms if t.kind != "company"]})
    tsq = _full_match_tsquery(word_groups, params) if word_groups.terms else ""
    company_ids = [t.company_id for t in query.terms if t.kind == "company" and t.company_id is not None]
    where, scope_params = scope_clause(scope)
    params.update(scope_params)
    conditions = [where]
    if tsq:
        conditions.append(_article_match(tsq))
    for index, company_id in enumerate(company_ids):
        params[f"cnt_company_{index}"] = company_id
        conditions.append(
            f"EXISTS (SELECT 1 FROM article_mentions cm WHERE cm.article_id = a.id"
            f" AND cm.company_id = %(cnt_company_{index})s)"
        )
    if not tsq and not company_ids:
        return {}
    rows = conn.execute(
        f"SELECT a.source_id, count(*) AS n FROM articles a WHERE {' AND '.join(conditions)} GROUP BY a.source_id",
        params,
    ).fetchall()
    return {row["source_id"]: int(row["n"]) for row in rows}


def _author_listing(conn, query: Query, scope: Scope, limit: int, offset: int) -> SearchResult:
    where, params = scope_clause(scope)
    params.update({"limit": limit, "offset": offset})
    rows = conn.execute(
        f"""
        SELECT a.id, a.title, a.source_id, a.published_at
        FROM articles a WHERE {where}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    ).fetchall()
    total = conn.execute(f"SELECT count(*) AS n FROM articles a WHERE {where}", scope_clause(scope)[1]).fetchone()
    hits = [ArticleHit(r["id"], r["title"], r["source_id"], r["published_at"], 0.0, True, []) for r in rows]
    counts = {}
    for row in conn.execute(
        f"SELECT a.source_id, count(*) AS n FROM articles a WHERE {where} GROUP BY a.source_id", scope_clause(scope)[1]
    ).fetchall():
        counts[row["source_id"]] = int(row["n"])
    return SearchResult(int(total["n"]), 0, hits, [], query, counts, available=int(total["n"]))


_CITE = re.compile(r"\[(\d{1,3})\]")


def cited_numbers(answer: str, count: int) -> list[int]:
    found: list[int] = []
    for match in _CITE.finditer(answer or ""):
        number = int(match.group(1))
        if 1 <= number <= count and number not in found:
            found.append(number)
    return found

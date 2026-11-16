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

from researchlib import embeddings
from researchlib.chunks import OCR_ORD_BASE, TRANSLATION_ORD_BASE, current_chunk_sql
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
class ScoreParameters:
    """按请求传入的评分参数；默认值与现有线上公式一致，不修改模块全局变量。"""

    k1: float = K1
    b: float = B
    title_tf: float = 0.5
    title_bonus: float = 0.3
    phrase_bonus: float = 0.5
    coverage_power: float = 2.0

    def __post_init__(self):
        ranges = {
            "k1": (0.1, 3.0), "b": (0.0, 1.0), "title_tf": (0.0, 2.0),
            "title_bonus": (0.0, 2.0), "phrase_bonus": (0.0, 2.0), "coverage_power": (0.5, 4.0),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{name} 必须在 {low}～{high} 之间")


@dataclass(frozen=True)
class ScoreContext:
    """评分所需的只读资料，可连同候选冻结到离线实验；时间也可冻结。"""

    avgdl: float
    company_rows: list[dict]
    depths: list[dict]
    deep_names: dict[int, list[str]]
    as_of: datetime | None = None


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

    def snippet_groups(self) -> list[tuple[float, list[str]]]:
        """检索页摆摘录窗口用：每个词组一项，(权重, 各种写法)。权重和打分一样按少见程度，公司名比「商业」「模式」重。"""
        groups: dict[int, tuple[float, list[str]]] = {}
        for term in self.terms:
            if term.group < 0:
                continue
            weight, forms = groups.get(term.group, (0.0, []))
            forms = forms + [form for form in (term.forms or [term.text]) if form and form not in forms]
            groups[term.group] = (max(weight, term.idf * term.weight), forms)
        return list(groups.values())


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
    # 语义相近的程度（0～1，按和问题的相似度折算，见 semantic_scores）。0 表示没有向量或按意思不相关。
    semantic: float = 0.0
    # 正文里没写全搜索词、靠语义检索进来的段落。页面上标「语义相关」。
    semantic_hit: bool = False
    # 这段有向量、算过和问题的相似度（semantic 为 0 表示按意思不相关，不是没算）。
    measured: bool = False
    # 交给模型时补上的前后文（同一篇相邻的块）。引用和链接仍落在这一块上。
    before: str = ""
    after: str = ""
    # 问题点到的公司里，这段写到了哪几家（公司 id，按问题里的顺序）。只问几家公司时按它轮流取，见 select。
    companies: list[int] = field(default_factory=list)

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
    # 只问一篇或一个目录时：这篇的章节标题、或目录下各篇的标题。模型据此知道自己看到的是哪一部分。
    outline: list[str] = field(default_factory=list)
    outline_label: str = ""
    # 只问一篇时：这篇一共多少字、交给模型的是不是全文。
    whole: bool = True


@dataclass
class ArticleHit:
    article_id: int
    title: str
    source_id: str
    published_at: object
    score: float
    full: bool
    passages: list[Passage]
    # 正文没写全搜索词，靠语义检索排进「全部命中」的。
    semantic: bool = False
    # 这篇没摘的备选段落（前面的卡片没摘过的），按分数排。页面上摘录一字不差时换用，见 _fresh_first。
    spare: list[Passage] = field(default_factory=list)


@dataclass
class SearchResult:
    total: int  # 全部词组都命中的文章数（精确计数）
    partial: int  # 候选里只命中一部分、但相关度够的文章数
    hits: list[ArticleHit]  # 这一页
    terms: list[str]  # 用来高亮的词
    query: Query | None
    counts: dict[str, int]  # 按来源分的全部命中篇数
    available: int = 0  # 能翻到的篇数（全部命中排在前，部分命中在后）
    semantic: int = 0  # 词没写全、按意思排进前面那一档的篇数（标「语义相关」），不算进 total
    scope: Scope | None = None  # counts 是在这个范围里数的（问题里点了作者时比页面选的窄）


_QUOTE = re.compile(r'"([^"]+)"|“([^”]+)”|「([^」]+)」|『([^』]+)』')
# 排除词：空格（或单行问题的开头）后面紧跟减号，减号后面紧跟汉字或字母。「利润 -30%」「A -> B」「-- 游戏」不算，
# 多行问题里行首的「-腾讯」是列表符，也不算。引号包着的 -"词组" 先认（_QUOTED_EXCLUDE），整个词组当排除词。
_EXCLUDE = re.compile(r"(?:^|(?<=[ \t\u3000]))[-－](?=[A-Za-z\u3400-\u9fff])([^\s]+)")
_EXCLUDE_LINES = re.compile(r"(?<=[ \t\u3000])[-－](?=[A-Za-z\u3400-\u9fff])([^\s]+)")
_QUOTED_EXCLUDE = re.compile(r'(?:^|(?<=\s))[-－](?:"([^"]+)"|“([^”]+)”|「([^」]+)」|『([^』]+)』)')
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_LATIN_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9.+\-]*$")
_CJK = re.compile(r"[㐀-鿿]")
_SPLITTER = "，"
# 文章指代必须直接处在比较句尾结构中，不能把日本文化、脚本文件里的「本文」当指代。
_ARTICLE_COMPARISON = re.compile(
    r"(?P<reference>当前(?:这篇(?:文章)?|(?:的)?文章)|这篇(?:文章)?|本篇(?:文章)?|本文)"
    r"(?:\s*的?\s*(?:核心)?(?:判断|观点|看法|结论|说法|论点|分析))?\s*"
    r"(?P<request>有(?:何|什么|哪些)(?:不同|差异|区别|异同)|(?:对比|比较)(?:一下)?)[？?。!！\s]*$"
)


def _article_comparison_spans(text: str, author_names: list[str]) -> list[tuple[int, int]]:
    """只掩去确认的指代和比较请求原坐标，词内同形文字及引号外的真实主题不动。"""
    match = _ARTICLE_COMPARISON.search(text)
    if match is None or not any(name in text for name in author_names):
        return []
    before = text[:match.start("reference")]
    if before and not (before[-1].isspace() or before[-1] in "，,；;。.!！？?：:、（(["):
        # 无空白的中文指代须有明确的「作者／观点 + 与／和／跟／同」边界。
        # 「日本文」「合同本文」即使紧挨比较请求，也不能从词内截掉「本文」。
        join = re.search(r"(?:与|和|跟|同)\s*$", before)
        left = before[:join.start()].rstrip() if join else ""
        subjects = [*author_names, *(name + "的" for name in author_names),
                    "判断", "观点", "看法", "结论", "说法", "论点", "分析"]
        if join is None or not any(left.endswith(subject) for subject in subjects):
            return []
    return [match.span("reference"), match.span("request")]


def _is_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


# —— 全库统计：块数、平均长度、各词出现的块数。缓存几分钟，库变了自己会过期。——

_stats_cache: dict[str, tuple[float, float]] = {}
_df_cache: dict[str, tuple[int, float]] = {}
_common_cache: dict[str, tuple[dict[str, int], float]] = {}
# 写法 → 交给全文索引的 tsquery 文本（去掉了切词补出来的子词）。分词参数固定，进程里缓存着就行。
_tsq_cache: dict[str, str] = {}
# 上面两个按查询词增长：网站公开、进程不重启就一直涨。超过这么多条整个清空再攒（重算一个词几毫秒）。
CACHE_MAX_ENTRIES = 20000


def _cache_put(cache: dict, key, value) -> None:
    if key not in cache and len(cache) >= CACHE_MAX_ENTRIES:
        cache.clear()
    cache[key] = value


# 块数在这以下时精确统计（几十毫秒）；更大的库用表的估计行数加抽样的平均长度，不再全表扫一遍。
EXACT_STATS_BELOW = 50000
STATS_SAMPLE_ROWS = 20000


def use_sampled_lexeme_df(estimate: int) -> bool:
    """大库才用 ANALYZE 的词频。小库块数是实数，词频也必须实数。

    导入过程中 autovacuum 可能先 ANALYZE 到一半：词频是当时的比例，pg_class.reltuples
    也还是当时的行数，再乘起来就比现在的实数小一截。块数用实数、词频用这个过期比例，
    IDF 会漂，同一份语料的检索排序能差出十几篇。
    """
    return estimate >= EXACT_STATS_BELOW


def _corpus_stats(conn) -> tuple[int, float]:
    now = time.monotonic()
    cached = _stats_cache.get("corpus")
    if cached and now - cached[1] < DF_TTL_S:
        total, avgdl = _stats_cache["corpus_values"]
        return int(total), avgdl
    row = conn.execute("SELECT reltuples::bigint AS n FROM pg_class WHERE oid = 'article_chunks'::regclass").fetchone()
    estimate = int(row["n"] or 0) if row else 0
    exact = not use_sampled_lexeme_df(estimate)
    if exact:
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
    _stats_cache["corpus_exact"] = exact
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
    只在大库使用。小库见 use_sampled_lexeme_df。
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
    # 小库的块数是实数。词频也实数，避免乘上导入中途 ANALYZE 留下的过期比例。
    _corpus_stats(conn)
    if len(term.forms) == 1 and not _stats_cache.get("corpus_exact"):
        lexeme = single_lexeme(_tsq_cache.get(term.forms[0], ""))
        if lexeme is not None:
            common = _common_lexemes(conn)
            if lexeme in common:
                found = min(max(total, 1), common[lexeme])
                _cache_put(_df_cache, key, (found, now))
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
    _cache_put(_df_cache, key, (found, now))
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
            _cache_put(_tsq_cache, form, reduce_phrase(row["t"]))
            usable.append(form)
        else:
            _cache_put(_tsq_cache, form, "")
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
    # 地址栏里的 %00 会变成 NUL 字符，Postgres 的文字不收，切词那一步就报错（检索页、问库都走这里）。
    raw = (question or "").replace("\x00", " ").strip()[:500]
    text = raw
    excludes: list[str] = []
    for match in _QUOTED_EXCLUDE.finditer(text):
        phrase = next(group for group in match.groups() if group).strip()
        if phrase and phrase not in excludes:
            excludes.append(phrase)
    text = _QUOTED_EXCLUDE.sub(" ", text)
    phrases: list[str] = []
    for match in _QUOTE.finditer(text):
        phrase = next(group for group in match.groups() if group)
        if phrase.strip() and phrase.strip() not in phrases:
            phrases.append(phrase.strip())
    text = _QUOTE.sub(" ", text)
    exclude_pattern = _EXCLUDE_LINES if "\n" in raw else _EXCLUDE
    for match in exclude_pattern.finditer(text):
        if match.group(1) and match.group(1) not in excludes:
            excludes.append(match.group(1))
    text = exclude_pattern.sub(" ", text)
    years = sorted({int(year) for year in _YEAR.findall(text)})

    rows = company_rows if company_rows is not None else load_company_rows(conn)
    matcher = cached_matcher("aho", rows) if rows else None
    by_id = {int(row["id"]): row for row in rows}
    author_names = sorted({source.author for source in sources if source.author}, key=len, reverse=True)
    comparison_spans = _article_comparison_spans(text, author_names)

    terms: list[Term] = []
    companies: list[dict] = []
    authors: list[str] = []
    pairs: list[tuple[str, str]] = []
    for index, phrase in enumerate(phrases):
        terms.append(Term(phrase, "phrase", index, forms=[phrase], weight=1.3))
    # 每个词自成一组（组与组之间是「且」）；用 or 连起来的片段并成一组（组内是「或」）。
    next_group = len(phrases)
    piece_group = -1
    piece_cursor = 0
    for piece, joined in _whitespace_pieces(text):
        piece_start = text.index(piece, piece_cursor)
        piece_cursor = piece_start + len(piece)
        shared = piece_group if joined and piece_group >= 0 else None
        masked = piece
        # 先按原坐标拿掉句尾指代和比较请求，长度不变，后面的公司坐标仍然对准原文。
        for start, end in comparison_spans:
            low, high = max(0, start - piece_start), min(len(piece), end - piece_start)
            if low < high:
                masked = masked[:low] + " " * (high - low) + masked[high:]
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
  nullif((SELECT tr.title_zh FROM article_translations tr WHERE tr.article_id = c.article_id
          AND tr.content_hash=a.content_hash), ''), a.title
) ELSE a.title END AS title,
a.source_id, a.published_at
"""


def _article_match(tsq: str, *, ocr: bool = False) -> str:
    """文章级的全文命中：原文的标题和正文，加上中文译文的标题和正文。

    写成几个子查询的并集，各自都能走 GIN 索引；写成 OR EXISTS 会退化成逐行过滤，库大了很慢。
    ocr：再算上图片里的字（lang = 'ocr' 的检索块）。只在截图里写了这个词的星球帖，结果里列着，
    数篇数时也要数到（「送货上门」结果 55 篇，来源计数只数到 16 篇）。
    """
    parts = [
        f"SELECT ai.id FROM articles ai WHERE ai.search_tsv @@ {tsq}",
        f"SELECT tq.article_id FROM article_translations tq JOIN articles ta ON ta.id=tq.article_id"
        f" WHERE tq.content_hash=ta.content_hash AND tq.search_tsv @@ {tsq}",
    ]
    if ocr:
        parts.append(f"SELECT oc.article_id FROM article_chunks oc WHERE oc.lang = 'ocr' AND oc.search_tsv @@ {tsq}")
    return f"a.id IN ({' UNION ALL '.join(parts)})"


def _prefer_translation(rows: list[dict], phrases: list[str] | None = None) -> list[dict]:
    """保留逐块证据；同篇有中文不能授权丢掉英文或OCR，重复片段由选择器处理。"""
    return rows


def _phrases(query: Query) -> list[str]:
    return [term.forms[0] for term in query.terms if term.kind == "phrase" and term.forms]


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
          WHERE {where} AND {current_chunk_sql()} AND c.search_tsv @@ {tsq}{extra}
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
            f" (SELECT tt.article_id FROM article_translations tt JOIN articles ta ON ta.id=tt.article_id"
            f" WHERE tt.content_hash=ta.content_hash AND tt.title_tsv @@ {tsq}))"
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
                   AND {current_chunk_sql('zc')}
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
        WHERE {current_chunk_sql()}
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
    # 问题里的公司是召回目标，页面显式选择的公司仍是边界，两者取交集。
    where, scope_params = scope_clause(scope)
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
              WHERE m.company_id = %(company)s {depth_filter} AND {where}
                AND {current_chunk_sql()} AND c.search_tsv @@ {tsq}
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
        WHERE {where} AND {current_chunk_sql()}
          AND (c.ord < 2 OR (c.ord >= %(zh_first)s AND c.ord < %(zh_first)s + 2))
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
        WHERE {where} AND {current_chunk_sql()}
        ORDER BY c.ord
        """,
        params,
    ).fetchall()
    return _prefer_translation(list(rows))


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
    return _prefer_translation(list(found.values()), _phrases(query))


# —— 语义召回 ——

# 向量这一路取多少块。问到公司时，大部分名额给提到这家公司的文章，少数留给没点名但谈的是同一件事的。
# 交给模型的份量更大时（问库后台 240 段），按份量放大，不然按意思相关、字面没写全的段落进不来。
VECTOR_POOL = 120
VECTOR_OPEN_POOL = 40
# 检索页列的是文章，一篇有一块对得上就够，向量这一路多取一些。
SEARCH_VECTOR_POOL = 240
# 语义分：问题和段落的余弦相似度，以第二近的那块为满分线（一块近乎照抄问题的段落不会把别的都压下去；
# 但不低于最近那块减去带宽），差出 SEMANTIC_BAND 以内的线性给 1～0 分，再远的 0 分。
# 不按名次截断：以前只有前 60 名有分，库到二十万块以后，关键词召回到的段落几乎全是 0 分，排序和摘录又退回只看字面。
# 带宽是余弦的单位，跟着向量模型走。bge-m3 上实测（20 个问题、1100 多段，交叉编码器打相关分）：
# 比第二近低 0.05 以内的段落 87% 相关，低 0.1～0.125 的 62%，低 0.175～0.2 的 40%，低 0.225 以上的不到 10%。
SEMANTIC_BAND = 0.2
# 问库时模型补的要点：放在 expansions 的这个键下；最多用几条，每条取多少块近邻，语义分打几折
# （要点是模型的话，不如问题本身可靠）。
ASPECTS_KEY = "__aspects__"
MAX_ASPECTS = 4
ASPECT_POOL = 60
ASPECT_WEIGHT = 0.85


# 先用 HNSW 索引取这么多倍的近邻，再按范围过滤；留下的不够数才逐块算距离。
ANN_OVERFETCH = 4


def _vector_filter(scope: Scope, company_ids: list[int] | None) -> tuple[str, dict]:
    where, params = scope_clause(scope)
    if company_ids:
        params["vec_companies"] = company_ids
        where += (
            " AND EXISTS (SELECT 1 FROM article_mentions vm WHERE vm.article_id = a.id"
            " AND vm.company_id = ANY(%(vec_companies)s))"
        )
    return where, params


def _vector_keys(conn, kind: str, vector: str, scope: Scope, limit: int, *, company_ids: list[int] | None = None,
                 model: str | None = None):
    """范围内和问题最相近的块。

    范围条件不和按距离排序写进同一条查询：HNSW 索引是近似的，带着范围条件走索引会漏；不走索引又要把范围内
    全部向量的距离算一遍，全库 10 万块要 0.5 秒，库越大越慢。（以前文章表的「可见」条件没有统计信息，规划器
    估成只剩 1 篇，总是选后一种；迁移 036 补上了。）所以分两步：先只在向量表上按索引取一批近邻，再查这些文章
    在不在范围里。过滤后不够数（范围很窄：某位作者、某段日期、提到某家公司的文章）再逐块精确算，见下面的写法。
    只问一篇或一个目录时直接逐块算。
    """
    where, params = _vector_filter(scope, company_ids)
    params['embedding_model'] = embeddings.model_name() if model is None else model
    if scope.article_id is None and scope.tree_id is None:
        pool = limit * ANN_OVERFETCH
        embeddings.tune_search(conn, pool)
        near = conn.execute(
            f"""
            SELECT e.article_id, e.ord, e.text_hash, 1 - (e.embedding <=> %(qv)s::{kind}) AS sim
            FROM chunk_embeddings e
            WHERE e.model=%(embedding_model)s
            ORDER BY e.embedding <=> %(qv)s::{kind}
            LIMIT %(ann_pool)s
            """,
            {"qv": vector, "ann_pool": pool, 'embedding_model': params['embedding_model']},
        ).fetchall()
        allowed = {
            (row['article_id'], row['ord']): row['text_hash']
            for row in conn.execute(
                f"SELECT x.article_id, x.ord, x.text_hash"
                " FROM unnest(%(ann_ids)s::bigint[],%(ann_ords)s::int[]) AS k(article_id,ord)"
                f" CROSS JOIN LATERAL ({_one_embedding_sql('e.text_hash', where)}) x",
                {**params, 'ann_ids': [row['article_id'] for row in near], 'ann_ords': [row['ord'] for row in near]},
            ).fetchall()
        }
        kept = sorted((row for row in near if (row['article_id'], row['ord']) in allowed
                       and row['text_hash'] == allowed[(row['article_id'], row['ord'])]),
                      key=lambda row: -float(row['sim']))
        if len(kept) >= limit:
            return {(row["article_id"], row["ord"]): float(row["sim"]) for row in kept[:limit]}
    params["qv"] = vector
    # 逐块算距离时，向量是不是这块现在的字（md5）只核排在前面的：以前写在条件里，范围内每一块都要读出整段字
    # 算一遍 md5，提到 Costco 的文章有五万块时这一步要六七百毫秒，算距离本身只要五六十毫秒。先按距离取两倍，
    # 核过的够数就停，不够再多取。取到的和每块都核一遍一样：核不过的块本来就不要，排序不变。
    # 先挑出范围内的文章，再逐篇按索引取它们的向量（LATERAL 加 OFFSET 0，规划器不能改连接顺序）。不这样写，
    # 规划器把向量表和段落块表按两列连接估成实际的五倍多，常常整张向量表扫一遍、每块回段落块表查一次：
    # 一个来源几百篇时要六百毫秒，逐篇取二十毫秒。排序的是子查询算出的距离，也用不上 HNSW 索引（近似的，
    # 加上范围条件会漏），这一步要的是范围内精确的最近。
    fetch = max(2 * limit, 64)
    while True:
        rows = conn.execute(
            f"""
            SELECT k.article_id, k.ord, 1 - k.dist AS sim, {embeddings.fresh_embedding_sql('k')} AS fresh
            FROM (
                SELECT x.article_id, x.ord, x.text_hash, x.dist
                FROM articles a
                CROSS JOIN LATERAL (
                    SELECT e.article_id, e.ord, e.text_hash, e.embedding <=> %(qv)s::{kind} AS dist
                    FROM chunk_embeddings e
                    JOIN article_chunks c ON c.article_id=e.article_id AND c.ord=e.ord
                    WHERE e.article_id = a.id AND e.model=%(embedding_model)s AND {current_chunk_sql()}
                    OFFSET 0
                ) x
                WHERE {where}
                ORDER BY x.dist
                LIMIT %(vec_fetch)s
            ) k
            JOIN article_chunks c ON c.article_id=k.article_id AND c.ord=k.ord
            JOIN articles a ON a.id = k.article_id
            ORDER BY k.dist
            """,
            {**params, "vec_fetch": fetch},
        ).fetchall()
        fresh = [row for row in rows if row["fresh"]]
        if len(fresh) >= limit or len(rows) < fetch:
            return {(row["article_id"], row["ord"]): float(row["sim"]) for row in fresh[:limit]}
        fetch *= 4


def semantic_candidates(
    conn, query: Query, scope: Scope, rows: list[dict], *, pool: int = VECTOR_POOL,
    aspects: list[str] | None = None,
) -> tuple[list[dict], dict[tuple[int, int], float]]:
    """向量这一路：补上关键词没召回到的段落，再给全部候选算语义分。

    aspects：问库时模型补的「要点」（一个好回答该覆盖的几个方面，各写成一句话）。每条要点自己算一次向量、
    取一批近邻，语义分按要点各自折算后打 ASPECT_WEIGHT 折，和问题本身的取大。问题很短（「costco 商业模式」）
    时，讲会员费、讲低毛利的段落离问题本身都不算最近，按要点找才进得来。

    没配向量服务、库里没有向量表、服务连不上时原样返回，语义分为空。
    """
    kind = embeddings.ready(conn)
    if kind is None:
        return rows, {}
    model = embeddings.model_name()
    vector = embeddings.query_vector(query.raw)
    if vector is None:
        return rows, {}
    if embeddings.model_name() != model:
        return rows, {}
    company_ids = [t.company_id for t in query.terms if t.kind == "company" and t.company_id is not None]
    aspect_texts = [text for text in dict.fromkeys((aspects or [])[:MAX_ASPECTS]) if text and text != query.raw]
    extra = [(text, embeddings.query_vector(text)) for text in aspect_texts]
    try:
        with conn.transaction():
            sims = _vector_lookup(conn, kind, embeddings.literal(vector), scope, rows, pool, company_ids, model=model)
            aspect_sims = [
                _vector_lookup(conn, kind, embeddings.literal(other), scope, rows, ASPECT_POOL, company_ids,
                               model=model)
                for _text, other in extra if other is not None
            ]
    except Exception as exc:  # 向量这一路出错不能拖垮检索：退回只用关键词。
        embeddings.logger.warning("语义召回失败，只用关键词：%s", exc)
        return rows, {}
    if embeddings.model_name() != model:
        return rows, {}
    have = {(row["article_id"], row["ord"]): row for row in rows}
    found_keys = list(dict.fromkeys([*sims, *(key for found in aspect_sims for key in found)]))
    fetched = {(row["article_id"], row["ord"]): dict(row)
               for row in _rows_for_keys(conn, [key for key in found_keys if key not in have])}
    # 正文几乎没字的块（只有链接、图片文件名），向量就是文章标题和章节标题的向量：标题和问题沾边，它就排在最前，
    # 还会把满分线抬高、把别的块都压下去，卡片和交给模型的原文却只有一串文件名。
    # 这样的块不算语义分（当没算过），关键词照常算。
    known = {**have, **fetched}
    sims = {key: sim for key, sim in sims.items() if key in known and not thin_text(known[key])}
    aspect_sims = [{key: sim for key, sim in found.items() if key in known and not thin_text(known[key])}
                   for found in aspect_sims]
    scores = semantic_scores(sims)
    for found in aspect_sims:
        for key, value in semantic_scores(found).items():
            scores[key] = max(scores.get(key, 0.0), ASPECT_WEIGHT * value)
    rows = list(rows) + [row for key, row in fetched.items() if scores.get(key, 0.0) > 0]
    return _prefer_translation(rows, _phrases(query)), scores


# 算「正文有没有字」时不算的：链接、图片和附件的文件名，截图的默认名（飞书导出的「截屏2025-06-08 19.36.10.png」、
# 「Screenshot 2024-06-17 at 16.18.55.png」，Stratechery 的「Screen Shot 2017-06-01 at 7.40.43 PM」）。
_NOT_TEXT = re.compile(
    r"https?://\S+|www\.\S+"
    r"|(?:Screen ?Shot|Screenshot|截屏|屏幕截图)\s*\d{4}-\d{1,2}-\d{1,2}(?:\s*at)?\s*\d{1,2}[.:]\d{2}[.:]\d{2}"
    r"(?:\s*[AP]M)?(?:\s*\(\d+\))?(?:\.[A-Za-z]{3,4})?"
    r"|[\w\-.%()（）]+\.(?:png|jpe?g|gif|webp|svg|bmp|heic|pdf|mp4|mov)\b",
    re.IGNORECASE,
)
# _NOT_TEXT 能命中的，正文里一定有下面这些字样之一（转小写后比）。大部分段落一个都没有，就不跑那条正则：
# 它在 Python 里每段要几十微秒，检索一次要对几千个候选段落各算一遍。
_NOT_TEXT_MARKERS = (
    "http", "www.", "screen", "截屏", "屏幕截图",
    ".png", ".jp", ".gif", ".webp", ".svg", ".bmp", ".heic", ".pdf", ".mp4", ".mov",
)
# 忽略大小写时和 ASCII 字母算同一个、转小写后却不是 ASCII 的几个字母（长 s、无点 i、带点 I、开尔文符号）。
# 正文里有它们时照旧跑正则，结果和不加这一步完全一样。
_CASEFOLD_ODD = ("ſ", "ı", "İ", "K")


def _drop_not_text(body: str) -> str:
    """等于 _NOT_TEXT.sub(" ", body)，只是没有那些字样时直接返回原文。"""
    lowered = body.lower()
    if any(marker in lowered for marker in _NOT_TEXT_MARKERS) or any(odd in body for odd in _CASEFOLD_ODD):
        return _NOT_TEXT.sub(" ", body)
    return body


# 汉字按字、英文按词数；数字和单个字母不算（截图名里的日期时刻、「0」这种残留）。
_TEXT_UNIT = re.compile(r"[一-鿿]|[A-Za-z]{2,}")
# 正文去掉上面这些以后不到这么多个字就算没字。
THIN_TEXT_UNITS = 5


def thin_text(row: dict) -> bool:
    """只看正文：章节标题是同一节各块共用的，有的导出把一整段话放进了章节标题，正文只剩图片文件名。"""
    return _body_features(row.get("body") or "")[1]


def _thin(text: str) -> bool:
    """去掉链接和文件名以后的正文不到 THIN_TEXT_UNITS 个字。数到够数就停，不用把整段数完。"""
    count = 0
    for _ in _TEXT_UNIT.finditer(text):
        count += 1
        if count >= THIN_TEXT_UNITS:
            return False
    return True


# —— 段落本身的信息量 ——
# 太短的段落写不下多少东西：一句话的帖子、只剩标题的块、聊天里的一句感叹。BM25 的长度归一本来就偏向短文本，
# 短到只剩问题里那几个词的块，关键词分和语义分都接近满分：线上搜「网络效应」，前十张卡片有三张正文只有一行字
# （「网络效应护城河」），盲评都是 0 分。正文不到这么多字的段落，分数乘 √(字数 / SUBSTANCE_UNITS)：
# 100 字乘 0.71，25 字乘 0.35。24 个问题盲评的 509 段里（2 有料、1 一般、0 没料），不到 60 字的 19 段没有一段
# 「有料」，平均 0.58 分；60～120 字的 30 段平均 1.40，120～200 字的 53 段 1.49，200 字以上的 407 段 1.69。
SUBSTANCE_UNITS = 200
# 英文一个词约合 1.6 个汉字：库里 Stratechery 的中英对照，37 篇译文汉字数和原文词数之比的中位数。
EN_WORD_UNITS = 1.6
# AI 研究报告导出的段落，句末带着一串「(12)」「(13)」引用编号，或者连写成「21212121」：多半是汇编来的定义和
# 常识复述。盲评的 509 段里这样的 11 段，10 段是「一般」。三处以上引用编号、或两处连写编号的段落再乘这个数。
CITED_SUMMARY = 0.8
_EN_WORD = re.compile(r"[A-Za-z]{2,}")
# 跟在句子后面的半角括号编号：「至关重要 (12)。」。全角的「（1）」是列点，表格里的「(245) |」是负数，都不算。
_CITATION = re.compile(
    r"(?<=[一-鿿。；，、”）A-Za-z0-9])\s?\(\d{1,3}(?:\s*[,，-]\s*\d{1,3})*\)(?=\s*(?:[。；，,.;：:\n]|$))"
)
_CITATION_RUN = re.compile(r"(?<!\d)(\d{2})\1{3,}(?!\d)")
# 连写编号至少八位数字连着：没有八位连着的数字就不用跑带回溯引用的 _CITATION_RUN。
_EIGHT_DIGITS = re.compile(r"\d{8}")


def substance(body: str) -> float:
    """段落本身信息量的系数，约 0.13～1：太短的打折（SUBSTANCE_UNITS），像 AI 汇编报告的再打折（CITED_SUMMARY）。

    只看正文，链接和文件名不算字，和 thin_text 一样不看章节标题。
    """
    return _body_features(body or "")[0]


def _substance(text: str) -> float:
    cjk = len(_CJK.findall(text))
    if cjk >= SUBSTANCE_UNITS:
        # 汉字已经够数，系数就是 1，英文词不用再数。
        factor = 1.0
    else:
        units = cjk + EN_WORD_UNITS * len(_EN_WORD.findall(text))
        factor = min(1.0, max(units, THIN_TEXT_UNITS) / SUBSTANCE_UNITS) ** 0.5
    # 两种编号都要半角括号或八位连着的数字，没有就不用跑那两条慢正则。
    if ("(" in text and len(_CITATION.findall(text)) >= 3) or (
        _EIGHT_DIGITS.search(text) is not None and len(_CITATION_RUN.findall(text)) >= 2
    ):
        factor *= CITED_SUMMARY
    return factor


# 一段正文的（信息量系数，是不是没字）只跟正文有关。检索一次要给几千个候选段落各算一遍，以前每段一百多微秒，
# 几千段就是半秒多，常常是整次检索里最慢的一步。同一段正文算过就记下来，下次检索直接用。
# 键是正文连同长度算出的 hash，不存正文本身，全库二十多万段也只占几十 MB；超过上限整个清空重来。
_FEATURES: dict[int, tuple[float, bool]] = {}
FEATURES_LIMIT = 400_000


def _body_features(body: str) -> tuple[float, bool]:
    key = hash((len(body), body))
    found = _FEATURES.get(key)
    if found is None:
        text = _drop_not_text(body)
        found = (_substance(text), _thin(text))
        if len(_FEATURES) >= FEATURES_LIMIT:
            _FEATURES.clear()
        _FEATURES[key] = found
    return found


def _vector_lookup(conn, kind: str, literal: str, scope: Scope, rows: list[dict], pool: int, company_ids: list[int],
                   *, model: str | None = None):
    model = embeddings.model_name() if model is None else model
    sims = _vector_keys(conn, kind, literal, scope, pool, company_ids=company_ids or None, model=model)
    if company_ids:
        for key, sim in _vector_keys(conn, kind, literal, scope, VECTOR_OPEN_POOL, model=model).items():
            sims.setdefault(key, sim)
    have = {(row["article_id"], row["ord"]) for row in rows}
    missing = [key for key in have if key not in sims]
    if missing:
        for row in conn.execute(
            f"""
            SELECT x.article_id, x.ord, x.sim
            FROM unnest(%(ids)s::bigint[], %(ords)s::int[]) AS k(article_id, ord)
            CROSS JOIN LATERAL ({_one_embedding_sql(f"1 - (e.embedding <=> %(qv)s::{kind}) AS sim")}) x
            """,
            {'qv': literal, 'ids': [key[0] for key in missing], 'ords': [key[1] for key in missing],
             'embedding_model': model},
        ).fetchall():
            sims[(row["article_id"], row["ord"])] = float(row["sim"])
    return sims


def _one_embedding_sql(columns: str, extra: str = "") -> str:
    """外层 unnest 出来的一个（文章, 块号）k 对应的向量行：这块是现行的，向量按现在的字算（current_embedding_sql），
    再加 extra（检索范围）。放进 CROSS JOIN LATERAL，加 OFFSET 0 不让规划器拆开重排，每个键都按主键直接找到那一行。
    写成普通连接时，规划器常常只拿文章号去查向量表的主键、再逐行比块号：长文一篇几百块，两三千个候选要翻
    一百多万行，四五百毫秒。"""
    return (
        f"SELECT e.article_id, e.ord, {columns}"
        " FROM chunk_embeddings e"
        " JOIN article_chunks c ON c.article_id = e.article_id AND c.ord = e.ord"
        " JOIN articles a ON a.id = e.article_id"
        f" WHERE e.article_id = k.article_id AND e.ord = k.ord AND {embeddings.current_embedding_sql()}"
        f"{' AND ' + extra if extra else ''}"
        " OFFSET 0"
    )


def semantic_scores(sims: dict[tuple[int, int], float]) -> dict[tuple[int, int], float]:
    """相似度换成 0～1 的语义分：和离问题第二近的那块比，差得越少分越高，差出 SEMANTIC_BAND 的为 0。

    每块都按自己的相似度给分，不按名次截断；以最近的块为准，不看绝对值（不同问题、不同模型的尺度不一样）。
    """
    if not sims:
        return {}
    top = sorted(sims.values(), reverse=True)[:2]
    # 满分线取第二近的那块，但不低于最近那块减去带宽：范围很窄、只有一块真正相近时，别的块不会跟着沾光。
    floor = max(top[-1], top[0] - SEMANTIC_BAND) - SEMANTIC_BAND
    # 差得太远的记 0 分（不是没有）：有向量、按意思不相关，挑选时据此把只沾了一两个词的段落筛掉。
    return {key: max(0.0, min(1.0, (sim - floor) / SEMANTIC_BAND)) for key, sim in sims.items()}


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


_REGEX_SPECIAL = re.compile(r"([\\^$.|?*+()\[\]{}])")


def _literal_sql(form: str, column: str, params: dict, key: str) -> str:
    """SQL 里「这段文字原样含有 form」，和 _form_counter 同一个口径：中文按字面，英文按词边界、不分大小写。"""
    if _is_cjk(form):
        params[key] = form
        return f"strpos({column}, %({key})s) > 0"
    params[key] = r"(?<![A-Za-z0-9])" + _REGEX_SPECIAL.sub(r"\\\1", form) + r"(?![A-Za-z0-9])"
    return f"({column}) ~* %({key})s"


def _count(text: str, lowered: str, form: str) -> int:
    if not form:
        return 0
    if _is_cjk(form):
        return text.count(form)
    pattern = r"(?<![A-Za-z0-9])" + re.escape(form.lower()) + r"(?![A-Za-z0-9])"
    return len(re.findall(pattern, lowered))


def _age_days(published, *, now: datetime | None = None) -> float | None:
    if published is None:
        return None
    if isinstance(published, datetime):
        moment = published if published.tzinfo else published.replace(tzinfo=UTC)
        return max(0.0, ((now or datetime.now(UTC)) - moment).total_seconds() / 86400)
    return None


def _year_of(published) -> int | None:
    if isinstance(published, datetime):
        return published.year
    return None


def score_context(conn, query: Query, rows: list[dict], scope: Scope, company_rows=None) -> ScoreContext:
    """只装载评分上下文；不迁移、写库、配置向量服务或调用模型。"""
    _total, avgdl = _corpus_stats(conn)
    focus_ids = [t.company_id for t in query.terms if t.kind == "company" and t.company_id is not None]
    if scope.company_id is not None and scope.company_id not in focus_ids:
        focus_ids.append(scope.company_id)
    companies = company_rows if company_rows is not None else (load_company_rows(conn) if focus_ids else [])
    deep_names: dict[int, list[str]] = {}
    if query.overview and not focus_ids and rows:
        for item in conn.execute(
            """
            SELECT m.article_id, c.name_zh, c.name_en, c.aliases
            FROM article_mentions m JOIN companies c ON c.id = m.company_id
            WHERE m.article_id = ANY(%s) AND m.depth <> 'passing'
            """, (sorted({row["article_id"] for row in rows}),),
        ).fetchall():
            names = [item["name_zh"], item["name_en"], *(item["aliases"] or [])]
            deep_names.setdefault(item["article_id"], []).extend(n for n in names if n and len(n) >= 2)
    depths = []
    if focus_ids and rows:
        depths = conn.execute(
            "SELECT article_id, company_id, depth FROM article_mentions"
            " WHERE article_id = ANY(%s) AND company_id = ANY(%s)",
            (sorted({row["article_id"] for row in rows}), focus_ids),
        ).fetchall()
    return ScoreContext(avgdl, companies, depths, deep_names)


def score_rows(
    conn,
    query: Query,
    rows: list[dict],
    *,
    scope: Scope,
    company_rows: list[dict] | None = None,
    semantic: dict[tuple[int, int], float] | None = None,
    parameters: ScoreParameters | None = None,
    context: ScoreContext | None = None,
):
    """semantic：{(文章, 块号): 语义分 0～1}。有它时关键词分和语义分按 LEXICAL_WEIGHT 融合，见 fuse_semantic。"""
    context = context or score_context(conn, query, rows, scope, company_rows)
    params = parameters or ScoreParameters()
    avgdl = context.avgdl
    focus_ids = [t.company_id for t in query.terms if t.kind == "company" and t.company_id is not None]
    if scope.company_id is not None and scope.company_id not in focus_ids:
        focus_ids.append(scope.company_id)
    focus_rows = [row for row in context.company_rows if int(row["id"]) in focus_ids]
    matcher = cached_matcher("focus", focus_rows) if focus_ids else None
    guarded = {int(row["id"]) for row in focus_rows if company_rules(row).get("needs")}
    deep_names = context.deep_names
    counters = {
        id(term): [(form, _form_counter(form)) for form in term.forms if form]
        for term in query.terms if term.kind != "company"
    }
    depths = {(item["article_id"], item["company_id"]): item["depth"] for item in context.depths}

    group_weight: dict[int, float] = {}
    for term in query.terms:
        if term.group < 0 or term.kind == "expansion":
            continue
        group_weight[term.group] = max(group_weight.get(term.group, 0.0), term.idf * term.weight)
    total_weight = sum(group_weight.values())
    phrases = [t for t in query.terms if t.kind == "phrase"]

    passages: list[Passage] = []
    multipliers: list[float] = []
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
        norm = params.k1 * (1 - params.b + params.b * dl / max(avgdl, 1))
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
            effective_tf = tf + params.title_tf * min(title_tf, 2)
            if effective_tf <= 0:
                continue
            score += term.weight * term.idf * effective_tf * (params.k1 + 1) / (effective_tf + norm)
            if title_tf:
                score += params.title_bonus * term.idf * term.weight
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
                score += params.phrase_bonus * (idf_a + idf_b)
        score *= 0.1 + 0.9 * coverage**params.coverage_power

        # 公司：段落里确实谈到了这家公司才算；整篇深入谈的文章再加一点。
        anchor = 1.0
        covered: list[int] = []
        if focus_ids:
            # 文章开头那块正文没再点名、标题点了的，也算写到了这家。
            leading = row["ord"] == _ord_range(row["ord"])[0]
            per_company = {
                cid: company_hits.get(cid, 0) or (1 if leading and title_companies.get(cid) else 0)
                for cid in focus_ids
            }
            covered = [cid for cid in focus_ids if per_company[cid]]
            hits = sum(per_company.values())
            # 问题点了公司，这段没写到它、所在文章也不是专门谈它的（标题或全文深入）：按意思再像，
            # 谈的多半是别家的同类事，排在谈这家公司的段落后面。整篇谈它的文章里没再点名的段落不算。
            if hits == 0 and scope.company_id is None and not any(
                depths.get((row["article_id"], cid)) in ("title", "whole") for cid in focus_ids
            ):
                anchor = UNANCHORED
            if query.overview:
                if hits == 0:
                    continue
                # 每家各算各的：这段写到它几次（取对数），乘这篇谈它有多深。只点一家时和以前一样；
                # 点了几家时，一段里顺带列了一串公司名的不会因为总次数多排到前面。
                score = sum(
                    math.log1p(count)
                    * _OVERVIEW_DEPTH[_DEPTH_RANK.get(depths.get((row["article_id"], cid), "passing"), 3)]
                    for cid, count in per_company.items()
                    if count
                )
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
            score = 1.0 / (1 + row['ord'] - _ord_range(row['ord'])[0])
            if any(name in text for name in deep_names.get(row["article_id"], ())):
                score *= 1.8
            coverage = 1.0
        key = (row["article_id"], row["ord"])
        measured = bool(semantic) and not query.overview and key in semantic
        sem = semantic.get(key, 0.0) if measured else 0.0
        if score <= 0 and sem <= 0:
            continue
        # 段落本身写了多少东西：关键词分和语义分都只管「谈没谈到」，一行字的帖子也能拿满分。
        mult = anchor * substance(body)
        age = _age_days(row.get("published_at"), now=context.as_of)
        if query.recent and age is not None:
            mult *= 0.55 + 0.9 * math.exp(-age / 400)
        if query.years:
            year = _year_of(row.get("published_at"))
            if year is not None:
                mult *= 1.6 if year in query.years else 0.6
        multipliers.append(mult)
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
                semantic=sem,
                measured=measured,
                companies=covered,
            )
        )
    return fuse_semantic(passages, multipliers)


# 关键词分和语义分的融合权重。同一批样本里，按融合分重排的 NDCG@15：只看关键词 0.85，权重 0.4～0.65 都在
# 0.915～0.926 之间，0.8 掉到 0.90。评判用的交叉编码器本身偏字面（讲网络效应却没写这四个字的段落只给 0.2），
# 取这一段里偏语义的一头。
LEXICAL_WEIGHT = 0.5
# 语义分到这个数，就当作「把问题谈到了」，挑选时不因为没写全搜索词被筛掉。
SEMANTIC_STRONG = 0.6
# 问题点了公司、这段和所在文章都没谈到它时，整段的分打这个折。
UNANCHORED = 0.5


def fuse_semantic(passages: list[Passage], multipliers: list[float]) -> list[Passage]:
    """关键词分按候选里的最高分归一，和语义分加权，再乘回时间加权。分数尺度保持和只用关键词时相近。

    没有语义分时原样返回（只乘时间加权）。靠语义进来的段落覆盖率按语义分算，挑选时不会因为没写全词被筛掉。
    """
    if not any(p.semantic > 0 for p in passages):
        for passage, mult in zip(passages, multipliers, strict=True):
            passage.score *= mult
        return passages
    top = max((p.score for p in passages), default=0.0) or 1.0
    for passage, mult in zip(passages, multipliers, strict=True):
        lexical = passage.score / top
        passage.score = mult * top * (LEXICAL_WEIGHT * lexical + (1 - LEXICAL_WEIGHT) * passage.semantic)
        passage.semantic_hit = passage.semantic >= SEMANTIC_STRONG and passage.coverage < 0.99
        passage.coverage = max(passage.coverage, passage.semantic)
    return passages


_DEPTH_RANK = {"title": 0, "whole": 1, "section": 2, "passing": 3}
# 只问公司时，按这篇谈这家公司的深度（标题、全文、单节、一带而过）给的倍数。
_OVERVIEW_DEPTH = (1.6, 1.4, 1.15, 0.5)


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


def _interleave(ranked: list[Passage], key, share=None) -> list[Passage]:
    """按 key 分组，各组轮流取；组内保持原来的先后，组按各自最高分排。share(组名) 是这一组每轮取几段，默认 1。"""
    buckets: dict[object, list[Passage]] = defaultdict(list)
    for passage in ranked:
        buckets[key(passage)].append(passage)
    order = sorted(buckets, key=lambda name: max(p.score for p in buckets[name]), reverse=True)
    queues = [(iter(buckets[name]), share(name) if share else 1) for name in order]
    interleaved: list[Passage] = []
    while queues:
        alive = []
        for queue, size in queues:
            taken = 0
            for passage in queue:
                interleaved.append(passage)
                taken += 1
                if taken >= size:
                    break
            if taken >= size:
                alive.append((queue, size))
        queues = alive
    return interleaved


def _bucket_key(passage: Passage, query: Query, sources_by_id: dict) -> object:
    if query.author_intent:
        source = sources_by_id.get(passage.source_id)
        return source.author if source else passage.source_id
    return _year_of(passage.published_at) or 0


# 一篇长文里有很多段都高度相关时（几十万字的研究），每篇 2～3 段的封顶会把它最有料的部分挡在外面。
# 这样的文章封顶放宽到 LONG_ARTICLE_FACTOR 倍；短文照旧。
LONG_ARTICLE_STRONG = 4
LONG_ARTICLE_FACTOR = 3


def article_caps(ranked: list[Passage], per_article: int) -> dict[int, int]:
    """每篇最多取几段。榜首一半分以上的段落，一篇里有 LONG_ARTICLE_STRONG 段以上，就多给名额。"""
    if not ranked:
        return {}
    top = max(p.score for p in ranked)
    strong: dict[int, int] = defaultdict(int)
    for passage in ranked:
        if passage.score >= top * 0.5:
            strong[passage.article_id] += 1
    return {
        article_id: min(per_article * LONG_ARTICLE_FACTOR, max(per_article, count))
        for article_id, count in strong.items()
        if count >= LONG_ARTICLE_STRONG
    }


def select(
    passages: list[Passage], query: Query, budget: Budget, sources_by_id: dict | None = None,
    *, min_score_ratio: float = 0.12,
) -> list[Passage]:
    if isinstance(min_score_ratio, bool) or not 0 <= min_score_ratio <= 0.5:
        raise ValueError("min_score_ratio 必须在 0～0.5 之间")
    ranked = sorted(passages, key=lambda p: (p.score, p.published_at is not None, p.published_at), reverse=True)
    if not ranked:
        return []
    if not query.overview:
        floor = ranked[0].score * min_score_ratio
        ranked = [p for p in ranked if p.score >= floor]
        # 按意思和问题不沾边（算过相似度、在带宽以外）、字面上也没写全的段落不要：多半只是碰上一两个常见词。
        # 交给模型的资料少一批噪音，分批读的批数也少。没算到向量的段落不受影响。
        if any(p.semantic > 0 for p in ranked):
            ranked = [p for p in ranked if not (p.measured and p.semantic <= 0 and p.coverage < 0.99)]
        # 全部词组都命中的段落已经不少时，只命中一半以下的就不要了，免得用噪音填满名额。
        full = sum(1 for p in ranked if p.coverage >= 0.99)
        if full >= max(10, budget.passages // 4):
            ranked = [p for p in ranked if p.coverage >= 0.5]
    # 梳理类按年份摊开，「哪些作者」按作者摊开；问「最近」时不摊年份，免得把旧文章顶上来。
    spread = query.author_intent or ((query.overview or query.timeline) and not query.recent)
    if spread:
        ranked = _interleave(ranked, lambda passage: _bucket_key(passage, query, sources_by_id or {}))
    if query.overview and len(query.companies) >= 2:
        # 只问几家公司（「美的和格力怎么比」）：同时写到几家的、只写到其中一家的轮流取，每一类里仍按年份摊开。
        # 不然被写得最多的那家占满名额：本地副本上「腾讯和网易」交给问库的 240 段里网易只有 3 段。
        # 写到几家的一轮取几段，每家露面的次数大致相同，同时谈到两家的比较段落占一半。
        ranked = _interleave(
            ranked, lambda passage: tuple(passage.companies), share=lambda covered: max(1, len(covered))
        )
    picked: list[Passage] = []
    per: dict[int, int] = {}
    caps = article_caps(ranked, budget.per_article)
    seen_text: set[str] = set()
    covered: set[str] = set()
    used = 0
    for passage in ranked:
        if per.get(passage.article_id, 0) >= caps.get(passage.article_id, budget.per_article):
            continue
        if picked and used + len(passage.body) > budget.chars:
            continue
        # 同一段话出现在两篇里（整段引用、没认出来的转发、抄进几篇笔记的同一份资料）只给一次，不占两个名额。
        # 只差开头结尾几个字的也算（Stratechery 几篇里重复的那段引文，后面跟着的链接字样不一样）：
        # 这段八成的字已经在前面挑中的段落里，就不要了。
        key = _text_key(passage.body)
        if key and key in seen_text:
            continue
        pieces = _shingles(key)
        if len(pieces) >= 4 and len(pieces & covered) >= REPEAT_SHARE * len(pieces):
            continue
        if key:
            seen_text.add(key)
        covered |= pieces
        per[passage.article_id] = per.get(passage.article_id, 0) + 1
        used += len(passage.body)
        picked.append(passage)
        if len(picked) >= budget.passages:
            break
    return picked


_TEXT_KEY = re.compile(r"[0-9A-Za-z一-鿿]+")
# 一段的字有这么多已经在前面挑中的段落里，就算重复。本地副本上问库后台的 240 段里，一段整个包含在另一段里、
# 或八成相同的约占 3%，「aggregation theory」占两成多。
REPEAT_SHARE = 0.8
# 按 50 个字一片比：同一段话（抄过去、转发、引文）连着几十个字都一样；只是用了几句相同的套话、
# 前后顺序不同的两段，50 个字的片很少对得上，不算重复。切成 10 个字一片时，检索评测语料里用同一批句式拼成的
# 相关段落被当成重复丢掉，召回率掉了一半以上。
_SHINGLE = 50


def _text_key(body: str) -> str:
    text = "".join(_TEXT_KEY.findall(body or "")).lower()
    return text if len(text) >= 30 else ""


def _shingles(key: str) -> set[str]:
    """归一后的正文按 _SHINGLE 个字一片切开，每个起点一片，全留。

    以前按 hash 留四分之一的片：Python 的字符串 hash 每次启动都不一样，同一个问题重启前后交给模型的段落会不同；
    短段落的片少，留下哪几片全凭运气，测试 400 个 PYTHONHASHSEED 里有 22 个对不上。全留就是准确的重合比例。
    """
    if not key:
        return set()
    return {key[i : i + _SHINGLE] for i in range(len(key) - _SHINGLE + 1)}


def _distinct_passages(items: list[Passage], limit: int) -> list[Passage]:
    """按分数顺序取前几块，和已取的块一字不差、或整块包含在里面（反过来也算）的跳过。"""
    kept: list[Passage] = []
    keys: list[str] = []
    for passage in items:
        key = _text_key(passage.body)
        if key and any(key in other or other in key for other in keys):
            continue
        kept.append(passage)
        if key:
            keys.append(key)
        if len(kept) >= limit:
            break
    return kept


def _scope_for_question(scope: Scope, query: Query, sources) -> Scope:
    """显式「只问这篇」保持读者选定的文章。没指定文章时，问题里的作者才收窄来源。"""
    if scope.article_id is not None or not query.authors or not scope.source_ids:
        return scope
    narrowed = [source.id for source in sources if source.author in query.authors and source.id in scope.source_ids]
    return replace(scope, source_ids=narrowed) if narrowed else scope


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
    # 模型补的要点不是检索词，只拿来按意思找（见 semantic_candidates），不进 analyze。
    aspects = [str(text) for text in (expansions or {}).get(ASPECTS_KEY) or [] if str(text).strip()]
    words = {key: value for key, value in (expansions or {}).items() if key != ASPECTS_KEY} or None
    query = analyze(conn, question, sources, company_rows=company_rows, expansions=words)
    # 明确的按日概览仍按日期取文章；内容词零命中不能据此改成概览。
    if (scope.date_from or scope.date_to) and re.fullmatch(
        r"(?:这一天|当天|这天)(?:大家)?(?:都)?在聊(?:什么|啥)[？?。!！\s]*", query.raw
    ):
        query = preset_query(query)
    # 语义检索只用在有具体内容的问题上：「汇总这一天」「库里怎么看腾讯」这类没有可比的语义。
    semantic_ok = not preset and any(t.kind in ("word", "phrase") for t in query.terms)
    semantic_only = semantic_ok and not any(t.df > 0 for t in query.terms if t.kind in ('word', 'phrase'))
    if preset:
        query = preset_query(query)
    else:
        # 问库是自然语言：库里一次都没出现过的词（「在聊」之类）对召回没用，还会让覆盖率永远凑不满。
        kept = [t for t in query.terms if t.kind != "word" or t.df > 0]
        if len(kept) != len(query.terms):
            content = [t for t in kept if t.kind in ("word", "phrase")]
            if content or not semantic_ok:
                query = replace(query, terms=kept, overview=not content)
    effective = _scope_for_question(scope, query, sources)
    sources_by_id = {source.id: source for source in sources}
    if scope.article_id is not None:
        return _retrieve_article(
            conn, query, effective, budget, purpose=purpose, semantic_ok=semantic_ok, aspects=aspects,
        )
    if scope.tree_id is not None:
        # 一个目录下往往就几篇长文：每篇封顶放宽，不然最长那篇只能进两三段。
        budget = replace(budget, per_article=max(budget.per_article, TREE_PER_ARTICLE))
    per_channel = min(CANDIDATES, max(400, budget.passages * 8))
    if semantic_ok and not query.overview and embeddings.ready(conn):
        # 问题（和要点）的向量在后台先算，和下面关键词召回的几条 SQL 同时进行。
        for text in [query.raw, *aspects[:MAX_ASPECTS]]:
            embeddings.prefetch_query_vector(text)
    rows = _candidates(conn, query, effective, limit=per_channel, enough=budget.passages * 6)
    if semantic_only:
        # 正文块词频不包含文章标题；只保留实际命中内容词的标题证据，不保留公司概览。
        rows = [row for row in rows if any(
            _count(row.get('title') or '', (row.get('title') or '').lower(), form)
            for term in query.terms if term.kind in {'word', 'phrase'} for form in term.forms if form
        )]
    semantic: dict[tuple[int, int], float] = {}
    if semantic_ok and not query.overview:
        rows, semantic = semantic_candidates(
            conn, query, effective, rows, pool=max(VECTOR_POOL, budget.passages), aspects=aspects,
        )
    scored = score_rows(conn, query, rows, scope=effective, company_rows=company_rows, semantic=semantic)
    picked = select(scored, query, budget, sources_by_id)
    # 具体内容在当前范围没有词汇或语义证据时，保持无证据。
    # 概览意图已在 analyze / preset_query 中确定，不能因召回为空而丢掉问题里的内容条件。
    if purpose in CONTEXT_PURPOSES:
        extra = budget.chars * (TREE_CONTEXT_SHARE if scope.tree_id is not None else CONTEXT_SHARE)
        attach_context(conn, picked, int(extra), span=2 if scope.tree_id is not None else 1)
    found = Retrieval(picked, query.companies, query.authors, effective, query.highlight_terms(), query, len(rows))
    if scope.tree_id is not None:
        found.outline = tree_titles(conn, effective)
        found.outline_label = "这个目录下的文章"
    return found


# 交给模型的用途才补前后文；检索页和「相关原文」列表只给命中的那一块。
CONTEXT_PURPOSES = frozenset({"llm", "deep"})
# 全库提问时，前后文最多再占预算的这么多；只问一篇或一个目录时多给。
CONTEXT_SHARE = 0.3
TREE_CONTEXT_SHARE = 0.6
TREE_PER_ARTICLE = 12
# 只问一篇：全文在这个字数以内就整篇给，超过就按问题定位。
CHAR_BUDGET_ARTICLE = 60000
# 只问一篇时页面上「相关原文」最多列这么多块。交给模型的不按块数截，只按字数。
DISPLAY_ARTICLE_CHUNKS = 40
# 只问一篇又没有具体问题（「概括这篇」）、后台分批读时，最多读这么多字，按原文顺序。
DEEP_ARTICLE_CHARS = 240000
# 只问一篇时按问题定位：命中的块先占这个比例的预算，剩下的给前后文。
ARTICLE_PICK_SHARE = 0.5


def _retrieve_article(
    conn, query: Query, scope: Scope, budget: Budget, *, purpose: str, semantic_ok: bool,
    aspects: list[str] | None = None,
) -> Retrieval:
    """只问这一篇。短文整篇按原文顺序给；长文按问题在这篇里检索，命中的块连同前后文按原文顺序给，再附上章节目录。"""
    rows = _article_rows(conn, scope)
    total = sum(len(r["body"]) for r in rows)
    outline = list(dict.fromkeys(r["heading"] for r in rows if r.get("heading")))
    terms = query.highlight_terms()

    def in_order(limit_chars: int, limit_count: int | None) -> list[Passage]:
        picked: list[Passage] = []
        used = 0
        for r in rows:
            if picked and ((limit_count and len(picked) >= limit_count) or used + len(r["body"]) > limit_chars):
                break
            used += len(r["body"])
            picked.append(_passage_from_row(r))
        return picked

    def result(picked: list[Passage], whole: bool) -> Retrieval:
        found = Retrieval(picked, query.companies, query.authors, scope, terms, query, len(rows), whole=whole)
        if not whole:
            found.outline = outline
            found.outline_label = "这篇的章节"
        return found

    # 页面列表才按块数截。交给模型的以前也截在 40 块（两万多字），两万到六万字的文章后半篇模型读不到，
    # 还被当成全文、不附章节目录，问到后半篇的内容就答「没有」。
    count = None if purpose in CONTEXT_PURPOSES else DISPLAY_ARTICLE_CHUNKS
    if total <= CHAR_BUDGET_ARTICLE:
        picked = in_order(CHAR_BUDGET_ARTICLE, count)
        return result(picked, len(picked) == len(rows))
    if query.overview:
        # 没有具体问题：后台分批读时按原文顺序读到上限，页面上先列开头。
        if purpose == "deep":
            picked = in_order(DEEP_ARTICLE_CHARS, None)
        else:
            picked = in_order(CHAR_BUDGET_ARTICLE, count)
        return result(picked, len(picked) == len(rows))
    candidates = _candidates(conn, query, scope, limit=2000, enough=2000)
    semantic: dict[tuple[int, int], float] = {}
    if semantic_ok:
        candidates, semantic = semantic_candidates(
            conn, query, scope, candidates, pool=max(VECTOR_POOL, budget.passages), aspects=aspects,
        )
    scored = score_rows(conn, query, candidates, scope=scope, semantic=semantic)
    chars = max(budget.chars, CHAR_BUDGET_ARTICLE)
    local = Budget(budget.passages, int(chars * ARTICLE_PICK_SHARE), budget.passages)
    picked = select(scored, query, local, {})
    if not picked:
        return result(in_order(CHAR_BUDGET_ARTICLE, count), False)
    if purpose in CONTEXT_PURPOSES:
        attach_context(conn, picked, chars - sum(len(p.body) for p in picked), span=2)
    picked.sort(key=lambda p: p.ord)
    return result(picked, False)


def attach_context(conn, picked: list[Passage], extra_chars: int, *, span: int = 1) -> int:
    """给挑中的段落补上同一篇前后各 span 块，按相关度从高到低补，直到用完 extra_chars。

    相邻的块本身也被挑中了就不重复补。原文块、译文块、图片文字块各自只和同类相邻。返回补了多少字。
    """
    if not picked or extra_chars <= 0:
        return 0
    chosen = {(p.article_id, p.ord) for p in picked}
    wanted: dict[tuple[int, int], None] = {}
    for passage in picked:
        low, high = _ord_range(passage.ord)
        for step in range(1, span + 1):
            for ord_ in (passage.ord - step, passage.ord + step):
                if low <= ord_ < high and (passage.article_id, ord_) not in chosen:
                    wanted[(passage.article_id, ord_)] = None
    if not wanted:
        return 0
    bodies = {
        (row["article_id"], row["ord"]): row["body"]
        for row in conn.execute(
            f"""
            SELECT c.article_id, c.ord, c.body
            FROM unnest(%s::bigint[], %s::int[]) AS k(article_id, ord)
            JOIN article_chunks c ON c.article_id = k.article_id AND c.ord = k.ord
            JOIN articles a ON a.id=c.article_id
            WHERE {current_chunk_sql()}
            """,
            ([key[0] for key in wanted], [key[1] for key in wanted]),
        ).fetchall()
    }
    used = 0
    taken: set[tuple[int, int]] = set()
    for passage in picked:
        before: list[str] = []
        after: list[str] = []
        for step in range(1, span + 1):
            for ord_, side in ((passage.ord - step, before), (passage.ord + step, after)):
                key = (passage.article_id, ord_)
                body = bodies.get(key)
                if not body or key in taken:
                    continue
                if used + len(body) > extra_chars:
                    break
                taken.add(key)
                used += len(body)
                side.append(body)
        passage.before = "\n".join(reversed(before))
        passage.after = "\n".join(after)
        if used >= extra_chars:
            break
    return used


def _ord_range(ord_: int) -> tuple[int, int]:
    if ord_ >= OCR_ORD_BASE:
        return OCR_ORD_BASE, 10**9
    if ord_ >= TRANSLATION_ORD_BASE:
        return TRANSLATION_ORD_BASE, OCR_ORD_BASE
    return 0, TRANSLATION_ORD_BASE


def tree_titles(conn, scope: Scope, limit: int = 200) -> list[str]:
    """目录下各篇的标题，按目录次序。"""
    where, params = scope_clause(scope)
    params["tree_limit"] = limit
    rows = conn.execute(
        f"""
        SELECT a.title
        FROM articles a
        LEFT JOIN doc_tree t ON t.article_id = a.id
        WHERE {where}
        ORDER BY (a.id = %(scope_tree)s) DESC, t.ord NULLS LAST, a.id
        LIMIT %(tree_limit)s
        """,
        params,
    ).fetchall()
    return [row["title"] for row in rows if row["title"]]




# —— 检索页：按文章聚合 ——


def _full_match_tsquery(query: Query, params: dict) -> str:
    return _group_tsquery(query, params, only_selective=False, operator="&&", prefix="full")


def rank_articles(scored: list[Passage], query: Query, company_rows: list[dict]) -> list[ArticleHit]:
    """文章聚合规则与线上检索共用；离线实验不复制另一套排序。"""
    by_article: dict[int, list[Passage]] = defaultdict(list)
    for passage in scored:
        by_article[passage.article_id].append(passage)
    groups = {t.group for t in query.terms if t.group >= 0}
    company_groups = {t.group for t in query.terms if t.group >= 0 and t.kind == "company"}
    titles = {p.article_id: p.title for p in scored}
    asked = {t.company_id for t in query.terms if t.kind == "company"}
    title_matcher = cached_matcher("focus", [row for row in company_rows if int(row["id"]) in asked]) if asked else None
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
            elif _title_mentions(term, title, title_matcher):
                matched_groups.add(term.group)
        full = groups <= matched_groups
        # 词没写全、但语义上就是在谈这件事的（「costco 商业模式」→ 讲会员费和低毛利的段落），
        # 问到的公司又确实提到了，也排进前面那一档，页面上标「语义相关」。
        by_meaning = (
            not full
            and company_groups <= matched_groups
            and max(p.semantic for p in items) >= SEMANTIC_STRONG
        )
        # 同一篇里重复的段落只算一次、只摘一次：Stratechery 的引文在正文里又以普通段落出现一遍，
        # 两块一字不差，页面上的两段片段就是同一段话。多留几段备选，见 _fresh_first。
        shown = _distinct_passages(items, CARD_CHOICES)
        score = shown[0].score + 0.3 * sum(p.score for p in shown[1:3])
        first = shown[0]
        hits.append(
            ArticleHit(
                article_id, title, first.source_id, first.published_at, score, full or by_meaning, shown,
                semantic=by_meaning,
            )
        )
    full_hits = sorted((h for h in hits if h.full), key=lambda h: h.score, reverse=True)
    partial_hits = sorted((h for h in hits if not h.full), key=lambda h: h.score, reverse=True)
    if partial_hits:
        best = partial_hits[0].score
        partial_hits = [h for h in partial_hits if h.score >= best * 0.2]
    shown_keys: set[str] = set()
    return _fresh_first(full_hits, shown_keys) + _fresh_first(partial_hits, shown_keys)


# 每篇留几段备选：前面的卡片已经摘过的段落，后面的卡片换一段摘。卡片上最多摘两段。
CARD_CHOICES = 6
CARD_PASSAGES = 2


def _fresh_first(hits: list[ArticleHit], shown_keys: set[str]) -> list[ArticleHit]:
    """同一段话原样出现在好几篇里，结果页上只摘一次。

    林文丰飞书里同一份资料抄进了几篇笔记，Stratechery 几篇文章里重复同一段引文：本地副本 20 万块正文里
    有 1.8 万块和别的文章一字不差。以前搜「Costco 商业模式」，前三张卡片摘的是同一段话。
    后面的卡片改摘这篇里别的、前面没摘过的段落；这篇命中的段落全是前面摘过的，卡片挪到这一档最后。
    shown_keys 记着已经摘过的段落，跨档沿用。

    这里只认整块一字不差的。两块不一样、页面上截出来的摘录却一样的（相邻两块重叠的那几句、同一段话前后
    多了几个字），由页面按显示出来的字再比一次，换用 spare 里的段落。
    """
    fresh_hits: list[ArticleHit] = []
    repeats: list[ArticleHit] = []
    for hit in hits:
        fresh = [p for p in hit.passages if not ((key := _text_key(p.body)) and key in shown_keys)]
        if fresh:
            hit.passages, hit.spare = fresh[:CARD_PASSAGES], fresh[CARD_PASSAGES:]
            fresh_hits.append(hit)
            shown_keys.update(key for p in hit.passages if (key := _text_key(p.body)))
        else:
            hit.passages = hit.passages[:CARD_PASSAGES]
            repeats.append(hit)
    return fresh_hits + repeats


def search_articles(conn, text: str, scope: Scope, sources, *, limit: int = 20, offset: int = 0) -> SearchResult:
    """关键词检索：全部词都命中的文章排前面，其后是只命中一部分的。每篇带最相关的两段原文。"""
    company_rows = load_company_rows(conn)
    query = analyze(conn, text, sources, company_rows=company_rows)
    effective = _scope_for_question(scope, query, sources)
    if not query.terms:
        if query.authors:
            return _author_listing(conn, query, effective, limit, offset)
        return SearchResult(0, 0, [], [], query, {})
    if query.content_terms and embeddings.ready(conn):
        # 问题的向量在后台先算，和下面关键词召回的几条 SQL 同时进行。
        embeddings.prefetch_query_vector(query.raw)
    rows = _candidates(conn, query, effective, limit=2000, enough=2000)
    semantic: dict[tuple[int, int], float] = {}
    if query.content_terms:
        rows, semantic = semantic_candidates(conn, query, effective, rows, pool=SEARCH_VECTOR_POOL)
    scored = score_rows(conn, query, rows, scope=effective, company_rows=company_rows, semantic=semantic)
    ordered = rank_articles(scored, query, company_rows)
    full_hits = [h for h in ordered if h.full]
    partial_hits = [h for h in ordered if not h.full]
    counts = full_match_counts(conn, query, effective)
    # 文章级索引要全部词落在同一处（原文、译文或同一张图）：词分在正文和截图里的那篇数不到，
    # 列表里却算全部命中。按来源取两边大的，总数就是来源计数之和，上下两个数对得上。
    # 语义相关的排在全部命中那一档，但词没写全，不数，另报。
    by_meaning = sum(1 for hit in full_hits if hit.semantic)
    listed: dict[str, int] = defaultdict(int)
    for hit in full_hits:
        if not hit.semantic:
            listed[hit.source_id] += 1
    for source_id, number in listed.items():
        counts[source_id] = max(counts.get(source_id, 0), number)
    total = sum(counts.values())
    ordered = full_hits + partial_hits
    return SearchResult(
        total=total,
        partial=len(partial_hits),
        hits=ordered[offset : offset + limit],
        terms=query.highlight_terms(),
        query=query,
        counts=counts,
        available=len(ordered),
        semantic=by_meaning,
        scope=effective,
    )


def _title_mentions(term: Term, title: str, matcher: FocusMatcher | None) -> bool:
    """标题里写没写到这个词组。和数段落时同一套规则：公司按别名数，英文按词边界。

    以前按子串找：「COST」（开市客的代码）算进了标题「The Cost of AI」，「DIS」算进了「Disruption」，
    没提这家公司的文章被当成问到的公司已经命中，排进前面那一档、标「语义相关」。
    """
    if not title:
        return False
    if term.kind == "company":
        return matcher is not None and matcher.count(title).get(term.company_id, 0) > 0
    lowered = title.lower()
    return any(_count(title, lowered, form) for form in term.forms if form)


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
        conditions.append(_article_match(tsq, ocr=True))
    for index, company_id in enumerate(company_ids):
        params[f"cnt_company_{index}"] = company_id
        conditions.append(
            f"EXISTS (SELECT 1 FROM article_mentions cm WHERE cm.article_id = a.id"
            f" AND cm.company_id = %(cnt_company_{index})s)"
        )
    # 引号里的词要原样出现，和结果列表同一条（score_rows 里没有原样写着这句的段落不要）：
    # 全文索引只认切出来的词，「"小程序://"」数到的是所有提到小程序的帖子：线上页面写着「196 篇全部命中，
    # 按相关度能翻到前 1 篇，其余的加来源或时间范围再搜」，列表却只收原样写着这句的段落。
    # 先用块的全文索引筛出含这几个词的块，再逐字核对，常见词的引号检索也不用把整篇每块都过一遍。
    for index, phrase in enumerate(_phrases(query)):
        words = _form_tsquery_sql([phrase], params, f"cnt_quote_words_{index}")
        literal = _literal_sql(phrase, "coalesce(pc.heading, '') || chr(10) || pc.body", params, f"cnt_quote_{index}")
        conditions.append(
            f"EXISTS (SELECT 1 FROM article_chunks pc WHERE pc.article_id = a.id"
            f" AND pc.search_tsv @@ {words} AND {literal})"
        )
    # 排除词也和结果列表同一条（score_rows 里写着排除词的段落不要）：每个词组都要在一块不含排除词的块里命中，
    # 或者写在标题里。以前不看排除词，线上「小程序 -广告」写着 569 篇全部命中，列表只有 504 篇，
    # 另外 65 篇写到小程序的每一块都写着广告，加来源、时间范围也翻不到。
    # 只点了公司（「腾讯 -游戏」）也一样：公司按别名算命中，每家公司要在一块不含排除词的块里写到，或者写在标题里。
    if query.excludes and query.terms:
        chunk_text = "coalesce(xc.heading, '') || chr(10) || xc.body"
        clean = " AND ".join(
            f"NOT ({_literal_sql(word, chunk_text, params, f'cnt_exclude_{index}')})"
            for index, word in enumerate(query.excludes)
        )
        by_group: dict[int, list[str]] = defaultdict(list)
        company_groups: set[int] = set()
        for term in query.terms:
            if term.group >= 0:
                by_group[term.group].extend(form for form in term.forms if form)
                if term.kind == "company":
                    company_groups.add(term.group)
        for group, forms in sorted(by_group.items()):
            if not forms:
                continue
            if group in company_groups:
                # 公司别名按字面找（和段落里数公司同一个口径），切词不一定切得出短别名。
                # 这一篇已由 article_mentions 筛过，只在它自己的块里找。
                written = " OR ".join(
                    _literal_sql(form, chunk_text, params, f"cnt_clean_{group}_{index}")
                    for index, form in enumerate(forms)
                )
                words_match = f"({written})"
            else:
                words_match = f"xc.search_tsv @@ {_form_tsquery_sql(forms, params, f'cnt_clean_{group}')}"
            titled = " OR ".join(
                _literal_sql(form, "coalesce(a.title, '')", params, f"cnt_title_{group}_{index}")
                for index, form in enumerate(forms)
            )
            conditions.append(
                f"({titled} OR EXISTS (SELECT 1 FROM article_chunks xc WHERE xc.article_id = a.id"
                f" AND {words_match} AND {clean}))"
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

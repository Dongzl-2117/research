"""对评测语料跑检索，按真值算准确率、召回率、覆盖面和耗时。

    python3 tools/retrieval_eval/gen_corpus.py --articles 6000   # 先造语料，写进 research_eval 库
    python3 tools/retrieval_eval/evaluate.py

库地址用 EVAL_URL 覆盖。改了检索之后跑一遍，看准确率和耗时有没有退步。
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from researchlib.db import ensure_search_session
from researchlib.sources import enabled_sources, load_sources

URL = os.environ.get("EVAL_URL", "postgresql://research:research@127.0.0.1:5432/research_eval")
ROOT = Path(__file__).resolve().parents[2]
SOURCES = enabled_sources(load_sources(ROOT / "config" / "sources.yaml"))
PRESET_COMPANY = "这家公司在库里被怎么讨论？按时间梳理各位作者的核心观点、关键数据和分歧，每条带出处。"
PRESET_AUTHOR = "这位作者近期最核心的判断有哪些？涉及哪些公司和行业？每条带出处。"


@dataclass
class Case:
    name: str
    question: str
    relevant: Callable[[dict], bool]
    author: str = ""
    company: str = ""
    keyword: bool = False  # 也拿去测检索页


CASES = [
    Case("作者+公司+主题", "方伟最近怎么看拼多多的护城河",
         lambda t: t["company"] == "拼多多" and t["concept"] == "护城河" and t["author"] == "方伟"),
    Case("公司+主题(关键词)", "腾讯广告加载率",
         lambda t: t["company"] == "腾讯" and t["concept"] == "广告加载率", keyword=True),
    Case("公司+两个主题", "茅台批价和渠道库存",
         lambda t: t["company"] == "贵州茅台" and t["concept"] in ("批价", "渠道库存"), keyword=True),
    Case("公司+主题", "苹果的定价权",
         lambda t: t["company"] == "苹果" and t["concept"] == "定价权", keyword=True),
    Case("主题·全面", "哪些作者讨论过回购", lambda t: t["concept"] == "回购"),
    Case("公司页预设·腾讯", PRESET_COMPANY,
         lambda t: t["company"] == "腾讯" and t["kind"] in ("topic", "company"), company="腾讯"),
    Case("作者页预设·乘风破浪", PRESET_AUTHOR,
         lambda t: t["author"] == "乘风破浪" and t["kind"] in ("topic", "company"), author="乘风破浪"),
    Case("公司+主题", "农夫山泉的成本结构",
         lambda t: t["company"] == "农夫山泉" and t["concept"] == "成本结构", keyword=True),
    Case("主题", "管理层激励", lambda t: t["concept"] == "管理层", keyword=True),
    Case("自然语言·同义", "低价电商能持续吗", lambda t: t["concept"] == "低价"),
    Case("公司+主题", "英伟达算力", lambda t: t["company"] == "英伟达" and t["concept"] == "算力", keyword=True),
    Case("公司+主题", "拼多多出海", lambda t: t["company"] == "拼多多" and t["concept"] == "出海", keyword=True),
]


def connect():
    conn = psycopg.connect(URL, row_factory=dict_row)
    ensure_search_session(conn)
    return conn


def truth(conn) -> dict[tuple[int, int], dict]:
    rows = conn.execute("SELECT * FROM eval_truth").fetchall()
    return {(row["article_id"], row["ord"]): row for row in rows}


def in_scope(t: dict, case: Case, company_articles: set[int]) -> bool:
    if case.author and t["author"] != case.author:
        return False
    if case.company and t["article_id"] not in company_articles:
        return False
    return True


def company_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM companies WHERE name_zh = %s", (name,)).fetchone()["id"]


def scope_for(conn, case: Case, scope_cls):
    ids = [s.id for s in SOURCES if not case.author or s.author == case.author]
    cid = company_id(conn, case.company) if case.company else None
    return scope_cls(source_ids=ids, company_id=cid)


def report_ask(name: str, run, conn, truth_map):
    print(f"\n== 问库召回：{name} ==")
    print(f"{'用例':<14}{'选中':>5}{'准确率':>8}{'召回率':>8}{'作者覆盖':>9}{'年份覆盖':>9}{'耗时ms':>8}")
    precisions = []
    for case in CASES:
        company_articles = set()
        if case.company:
            cid = company_id(conn, case.company)
            company_articles = {
                r["article_id"] for r in conn.execute(
                    "SELECT article_id FROM article_mentions WHERE company_id = %s", (cid,)
                ).fetchall()
            }
        relevant = {k for k, t in truth_map.items() if case.relevant(t) and in_scope(t, case, company_articles)}
        started = time.perf_counter()
        picked = run(conn, case)
        elapsed = (time.perf_counter() - started) * 1000
        hits = [key for key in picked if key in relevant]
        precision = len(hits) / len(picked) if picked else 0.0
        recall = len(set(hits)) / len(relevant) if relevant else 0.0
        authors_all = {truth_map[k]["author"] for k in relevant}
        years_all = {truth_map[k]["year"] for k in relevant}
        authors_hit = {truth_map[k]["author"] for k in hits}
        years_hit = {truth_map[k]["year"] for k in hits}
        precisions.append(precision)
        print(
            f"{case.name:<14}{len(picked):>5}{precision:>8.0%}{recall:>8.1%}"
            f"{len(authors_hit):>4}/{len(authors_all):<4}{len(years_hit):>4}/{len(years_all):<4}{elapsed:>8.0f}"
            f"   {case.question[:18]}"
        )
    print(f"平均准确率 {statistics.mean(precisions):.0%}")


def report_search(name: str, run, conn, truth_map):
    print(f"\n== 检索页：{name}（文章级，前 20 篇） ==")
    print(f"{'查询':<16}{'命中篇数':>8}{'前20准确率':>10}{'耗时ms':>8}")
    relevant_articles_by_case = {}
    for case in [c for c in CASES if c.keyword]:
        relevant_articles = {k[0] for k, t in truth_map.items() if case.relevant(t)}
        relevant_articles_by_case[case.question] = relevant_articles
        started = time.perf_counter()
        total, ids = run(conn, case.question)
        elapsed = (time.perf_counter() - started) * 1000
        good = [i for i in ids if i in relevant_articles]
        precision = len(good) / len(ids) if ids else 0.0
        print(
            f"{case.question:<16}{total:>8}{precision:>10.0%}{elapsed:>8.0f}"
            f"   相关文章共 {len(relevant_articles)} 篇"
        )


def new_ask(conn, case: Case):
    from researchlib.retrieval import retrieve
    from researchlib.web.queries import Scope

    scope = scope_for(conn, case, Scope)
    preset = case.question in (PRESET_COMPANY, PRESET_AUTHOR)
    found = retrieve(conn, case.question, scope, SOURCES, purpose="llm", preset=preset)
    return [(p.article_id, p.ord) for p in found.passages]


def new_search(conn, query: str):
    from researchlib.retrieval import search_articles
    from researchlib.web.queries import Scope

    scope = Scope(source_ids=[s.id for s in SOURCES])
    result = search_articles(conn, query, scope, SOURCES, limit=20, offset=0)
    return result.total, [hit.article_id for hit in result.hits]


def main() -> int:
    conn = connect()
    truth_map = truth(conn)
    report_ask("问库召回（交给模型的段落）", new_ask, conn, truth_map)
    report_search("检索页", new_search, conn, truth_map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

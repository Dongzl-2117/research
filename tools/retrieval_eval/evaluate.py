"""对评测语料跑检索，按真值算准确率、召回率、覆盖面和耗时。

    python3 tools/retrieval_eval/gen_corpus.py --articles 6000   # 先造语料，写进 research_eval 库
    python3 tools/retrieval_eval/evaluate.py

库地址用 EVAL_URL 覆盖。改了检索之后跑一遍，看准确率和耗时有没有退步。

CI 用 1500 篇的语料跑，和 baseline.json 比：哪个用例的准确率或召回率比基线低出容差，就失败。
检索改好了、数字变高了，用 --json 重写基线：

    python3 tools/retrieval_eval/gen_corpus.py --articles 1500
    python3 tools/retrieval_eval/evaluate.py --check tools/retrieval_eval/baseline.json
    python3 tools/retrieval_eval/evaluate.py --json tools/retrieval_eval/baseline.json

耗时只打印，不比：CI 机器的快慢每次不一样。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from researchlib.ask import PRESETS
from researchlib.db import ensure_search_session
from researchlib.sources import enabled_sources, load_sources

URL = os.environ.get("EVAL_URL", "postgresql://research:research@127.0.0.1:5432/research_eval")
ROOT = Path(__file__).resolve().parents[2]
SOURCES = enabled_sources(load_sources(ROOT / "config" / "sources.yaml"))
# 和公司页、作者页上的「问 AI」用同一句预设问题。
PRESET_COMPANY = PRESETS["company"]
PRESET_AUTHOR = PRESETS["author"]
# 准确率、召回率比基线低出这么多算退步。
TOLERANCE = 0.02


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


def case_key(case: Case) -> str:
    """基线里用的名字。预设问题是长句，用用例名代替。"""
    return case.name if case.question in (PRESET_COMPANY, PRESET_AUTHOR) else case.question


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


def measure_ask(run, conn, truth_map) -> list[dict]:
    results = []
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

        def spread(field: str, keys) -> int:
            return len({truth_map[key][field] for key in keys})

        results.append(
            {
                "case": case,
                "picked": len(picked),
                "precision": len(hits) / len(picked) if picked else 0.0,
                "recall": len(set(hits)) / len(relevant) if relevant else 0.0,
                "authors": (spread("author", hits), spread("author", relevant)),
                "years": (spread("year", hits), spread("year", relevant)),
                "ms": elapsed,
            }
        )
    return results


def measure_search(run, truth_map) -> list[dict]:
    results = []
    for case in [c for c in CASES if c.keyword]:
        relevant_articles = {k[0] for k, t in truth_map.items() if case.relevant(t)}
        started = time.perf_counter()
        total, ids = run(case.question)
        elapsed = (time.perf_counter() - started) * 1000
        good = [i for i in ids if i in relevant_articles]
        results.append(
            {
                "case": case,
                "total": total,
                "precision": len(good) / len(ids) if ids else 0.0,
                "relevant": len(relevant_articles),
                "ms": elapsed,
            }
        )
    return results


def print_ask(results: list[dict]) -> None:
    print("\n== 问库召回（交给模型的段落） ==")
    print(f"{'用例':<14}{'选中':>5}{'准确率':>8}{'召回率':>8}{'作者覆盖':>9}{'年份覆盖':>9}{'耗时ms':>8}")
    for item in results:
        case = item["case"]
        print(
            f"{case.name:<14}{item['picked']:>5}{item['precision']:>8.0%}{item['recall']:>8.1%}"
            f"{item['authors'][0]:>4}/{item['authors'][1]:<4}{item['years'][0]:>4}/{item['years'][1]:<4}"
            f"{item['ms']:>8.0f}   {case.question[:18]}"
        )
    print(f"平均准确率 {statistics.mean(item['precision'] for item in results):.0%}")


def print_search(results: list[dict]) -> None:
    print("\n== 检索页（文章级，前 20 篇） ==")
    print(f"{'查询':<16}{'命中篇数':>8}{'前20准确率':>10}{'耗时ms':>8}")
    for item in results:
        print(
            f"{item['case'].question:<16}{item['total']:>8}{item['precision']:>10.0%}{item['ms']:>8.0f}"
            f"   相关文章共 {item['relevant']} 篇"
        )


def summarize(articles: int, ask: list[dict], search: list[dict]) -> dict:
    return {
        "articles": articles,
        "tolerance": TOLERANCE,
        "ask": {
            case_key(item["case"]): {"precision": round(item["precision"], 4), "recall": round(item["recall"], 4)}
            for item in ask
        },
        "search": {item["case"].question: {"precision": round(item["precision"], 4)} for item in search},
    }


def compare(current: dict, baseline: dict) -> tuple[list[str], list[str]]:
    """返回 (退步, 变好)。用例在基线里没有的只提示，不算退步。"""
    tolerance = float(baseline.get("tolerance", TOLERANCE))
    worse: list[str] = []
    better: list[str] = []
    for part, label in (("ask", "问库"), ("search", "检索页")):
        for key, metrics in current.get(part, {}).items():
            base = baseline.get(part, {}).get(key)
            if base is None:
                better.append(f"{label}「{key}」基线里没有，用 --json 补上")
                continue
            for metric, value in metrics.items():
                expected = base.get(metric)
                if expected is None:
                    continue
                name = {"precision": "准确率", "recall": "召回率"}.get(metric, metric)
                if value < expected - tolerance:
                    worse.append(f"{label}「{key}」{name} {value:.1%}，基线 {expected:.1%}")
                elif value > expected + tolerance:
                    better.append(f"{label}「{key}」{name} {value:.1%}，基线 {expected:.1%}")
        for key in baseline.get(part, {}):
            if key not in current.get(part, {}):
                worse.append(f"{label}「{key}」这次没有跑")
    return worse, better


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索评测")
    parser.add_argument("--check", type=Path, help="和这份基线比，有用例退步就返回 1")
    parser.add_argument("--json", type=Path, help="把这次的准确率、召回率写成基线")
    args = parser.parse_args(argv)
    conn = connect()
    truth_map = truth(conn)
    articles = conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
    ask = measure_ask(new_ask, conn, truth_map)
    search = measure_search(lambda query: new_search(conn, query), truth_map)
    print_ask(ask)
    print_search(search)
    current = summarize(articles, ask, search)
    if args.json:
        args.json.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n基线写到 {args.json}")
    if args.check:
        baseline = json.loads(args.check.read_text(encoding="utf-8"))
        if baseline.get("articles") != articles:
            print(f"\n语料 {articles} 篇，基线是按 {baseline.get('articles')} 篇算的，不能比。", file=sys.stderr)
            return 1
        worse, better = compare(current, baseline)
        for line in better:
            print(f"变好：{line}")
        if worse:
            print("\n检索退步了：", file=sys.stderr)
            for line in worse:
                print(f"  {line}", file=sys.stderr)
            return 1
        print("\n和基线比没有退步。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

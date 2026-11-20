"""刚抓进来的文章当场处理完：识图、译制、判断、摘要、段落打分（research-process-new）。

为什么要它（2026-11-19，Zhilin：「以后新抓取的翻译、操作、打标签不需要等便宜时段，直接来，只有大面积操作才需要等」）：
以前公众号、星球的定时抓取导入以后只补摘要，判断、段落打分、识图都等当晚 00:37 的夜间任务，还要和积压一起
按每晚的上限排队；DeepSeek 高峰价时段（北京时间工作日 9 到 12 点、14 到 18 点）按队列译的不开新篇。
新文章一天几篇到十几篇，高峰价多花的是几分钱，不值得让读者等半天。

现在抓取脚本导入前记下库里最大的文章 id（``--last-id``），导入以后对这一来源新进来的几篇按顺序做：

1. 识图（``--ocr``，跟着夜间任务的 NIGHTLY_OCR 开关）：只认这几篇里没认过的图，摘要和判断第一遍就读得到图里的字。
2. 译制：其中的英文文章点名译（不避开高峰价）。
3. 判断：深入谈到公司的抽观点。
4. 摘要。
5. 段落打分：还没译完的英文文章先不打（译完以后只打中文块，免得英文块白打一遍），留给夜间。

每一步只挑这几篇里还该做的（各自原来的条件：网站上显示、主版本、不在失败冷却里、没做过或过期了），
一篇出错记下来接着做下一篇，一步出错接着做下一步。新文章之外的积压照旧由夜间任务按每晚上限补，
全库重跑照旧先报费用、等 Zhilin 同意，带 ``--off-peak`` 避开高峰。

    research-process-new --last-id                                   # 印出库里最大的文章 id
    research-process-new --source latepost --after-id 24100          # 处理这一来源 id 比它大的文章
    research-process-new --source fangwei-zsxq --after-id 24100 --ocr

退出码：0 做完了（个别文章失败、接口暂时不通也算，没做完的夜间再补）；2 没有可用的 LLM 凭证、鉴权被拒或余额不足；
1 某一步出了代码错误。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import replace

from researchlib import llm_batch
from researchlib.author_voice import site_visible_sql
from researchlib.config import Settings
from researchlib.visibility import visible_sql

# 一次最多处理这么多篇新文章：防的是导入了一大批（回填、整目录重新导入认出一批新的）也在抓取脚本里当场做完。
# 多出来的按发布时间留给夜间任务，大面积的照旧先报费用。
DEFAULT_LIMIT = 30
# 段落打分和夜间一样只问一档低思考。
MECHANISM_EFFORT = "low"


def last_article_id(conn) -> int:
    row = conn.execute("SELECT coalesce(max(id), 0) AS id FROM articles").fetchone()
    return int(row["id"])


def new_article_ids(conn, source_ids: list[str], after_id: int, limit: int) -> tuple[list[int], int]:
    """这些来源里 id 比 after_id 大、网站上显示的主版本，新的在前最多 limit 篇。交回 (id 列表, 一共几篇)。"""
    rows = conn.execute(
        f"""
        SELECT a.id
        FROM articles a
        WHERE a.source_id = ANY(%s) AND a.id > %s AND a.duplicate_of IS NULL
          AND {visible_sql("a")} AND {site_visible_sql("a")}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        """,
        (source_ids, after_id),
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    return ids[:limit], len(ids)


def _report(name: str, report: dict, verb: str) -> int:
    print(f"{name}：{verb} {report['done']} 篇，失败 {report['failed']} 篇" + llm_batch.tail(report, verb))
    return llm_batch.exit_code(report, verb)


def step_ocr(ids: list[int]) -> int:
    from researchlib import ocr

    argv = ["--limit", "200"]
    for article_id in ids:
        argv += ["--article", str(article_id)]
    return ocr.main(argv, limit_followups_to_named=True)


def step_translate(settings: Settings, ids: list[int], workers: int) -> int:
    from researchlib.db import prepare
    from researchlib.translate import cli
    from researchlib.translate.service import list_pending_english

    conn = prepare(settings)
    try:
        english = list_pending_english(conn, limit=len(ids), article_ids=ids)
        conn.commit()
    finally:
        conn.close()
    if not english:
        print("译制：没有要译的英文文章")
        return 0
    # 点名译：不避开高峰价，被断线、限流打断的隔几分钟自己再补两轮。
    argv = ["--workers", str(max(1, min(workers, len(english))))]
    for article_id in english:
        argv += ["--article-id", str(article_id)]
    print(f"译制：{len(english)} 篇英文文章")
    return cli.main(argv)


def step_views(settings: Settings, ids: list[int], authors: dict[str, str], workers: int) -> int:
    from researchlib import views
    from researchlib.db import prepare

    conn = prepare(settings)
    try:
        wanted = [int(row["id"]) for row in views.view_companies(conn, list(authors))]
        rows = views.pending_articles(conn, wanted, list(authors), len(ids), article_ids=ids) if wanted else []
        jobs = [
            {
                "id": int(row["id"]),
                "title": row["title"],
                "source_id": row["source_id"],
                "companies": list(row["companies"]),
            }
            for row in rows
        ]
    finally:
        conn.close()
    if not jobs:
        print("判断：没有深入谈到公司、要整理的")
        return 0
    return _report("判断", views.run_batch(settings, jobs, authors, workers=workers), "整理")


def step_digest(settings: Settings, ids: list[int], authors: dict[str, str], workers: int) -> int:
    from researchlib import digest
    from researchlib.db import prepare

    conn = prepare(settings)
    try:
        rows = digest.pending_articles(conn, list(authors), len(ids), article_ids=ids)
        jobs = [{"id": int(row["id"]), "title": row["title"]} for row in rows]
    finally:
        conn.close()
    if not jobs:
        print("摘要：没有要写的")
        return 0
    return _report("摘要", digest.run_batch(settings, jobs, authors, workers=workers), "写")


def step_mechanism(settings: Settings, ids: list[int], source_ids: list[str], workers: int) -> int:
    from researchlib import mechanism
    from researchlib.db import prepare
    from researchlib.translate.service import list_pending_english

    conn = prepare(settings)
    try:
        # 还没译完的英文文章先不打：译完以后英文块不打、只打中文块，现在打就白打一遍英文。
        english = set(list_pending_english(conn, limit=len(ids), article_ids=ids))
        keep = [article_id for article_id in ids if article_id not in english]
        rows = mechanism.pending_articles(conn, source_ids, None, article_ids=keep) if keep else []
        jobs = [{"id": int(row["id"]), "title": row["title"]} for row in rows]
        conn.commit()
    finally:
        conn.close()
    if english:
        print(f"段落打分：{len(english)} 篇英文还没译完，留给夜间")
    if not jobs:
        print("段落打分：没有要打的")
        return 0
    scored = replace(settings, llm_effort=MECHANISM_EFFORT)
    return _report("段落打分", mechanism.run_batch(scored, jobs, workers=workers), "打分")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="刚抓进来的文章当场识图、译制、整理判断、写摘要、给段落打分（花模型钱）"
    )
    parser.add_argument(
        "--last-id", action="store_true", help="只印出库里最大的文章 id（抓取前记下，导入后传给 --after-id）"
    )
    parser.add_argument("--source", action="append", default=[], help="来源 id，可重复")
    parser.add_argument("--after-id", type=int, default=None, help="只处理 id 比它大的文章")
    parser.add_argument("--ocr", action="store_true", help="先认这几篇里没认过的图（跟着夜间任务的 NIGHTLY_OCR）")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"最多处理几篇（默认 {DEFAULT_LIMIT}），多的留给夜间"
    )
    parser.add_argument("--workers", type=int, default=4, help="几篇同时处理（默认 4）")
    args = parser.parse_args(argv)
    from researchlib.db import prepare
    from researchlib.llm import LLMUnavailable, ensure_oauth
    from researchlib.sources import enabled_sources, load_sources

    settings = Settings.from_env()
    if args.last_id:
        conn = prepare(settings)
        try:
            print(last_article_id(conn))
        finally:
            conn.close()
        return 0
    if not args.source or args.after_id is None:
        parser.error("要写 --source 和 --after-id（或者只用 --last-id）")
    sources = {source.id: source.author for source in enabled_sources(load_sources(settings.sources_path))}
    unknown = [item for item in args.source if item not in sources]
    if unknown:
        raise SystemExit(f"{'、'.join(unknown)} 不是已启用的来源")
    authors = {item: sources[item] for item in args.source}
    conn = prepare(settings)
    try:
        ids, total = new_article_ids(conn, list(authors), args.after_id, max(1, args.limit))
    finally:
        conn.close()
    if not ids:
        print("没有新文章")
        return 0
    shown = "、".join(str(article_id) for article_id in ids)
    print(f"新文章 {total} 篇，这次处理 {len(ids)} 篇：{shown}")
    if total > len(ids):
        print(f"另外 {total - len(ids)} 篇留给夜间任务按每晚上限补")
    try:
        ensure_oauth(settings)
    except LLMUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    workers = max(1, min(args.workers, int(getattr(settings, "llm_concurrency", 8) or 8), llm_batch.MAX_WORKERS))

    steps = []
    if args.ocr:
        steps.append(("识图", lambda: step_ocr(ids)))
    steps += [
        ("译制", lambda: step_translate(settings, ids, workers)),
        ("判断", lambda: step_views(settings, ids, authors, workers)),
        ("摘要", lambda: step_digest(settings, ids, authors, workers)),
        ("段落打分", lambda: step_mechanism(settings, ids, list(authors), workers)),
    ]
    fatal = ""
    broken: list[str] = []
    for name, run in steps:
        try:
            code = run()
        except Exception as exc:  # 一步出错不连累后面几步
            traceback.print_exc()
            broken.append(f"{name}（{type(exc).__name__}: {exc}）")
            continue
        if code == 2:
            # 没凭证、鉴权被拒、余额不足：后面几步也一样过不去。
            fatal = name
            break
        if code not in (0, llm_batch.EXIT_PAUSED):
            broken.append(f"{name}（退出码 {code}）")
    if fatal:
        print(f"{fatal}整批停下（凭证、鉴权或余额），后面几步没做，夜间任务再补", file=sys.stderr)
        return 2
    if broken:
        print(f"出错的步骤：{'；'.join(broken)}。没做完的夜间任务再补", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

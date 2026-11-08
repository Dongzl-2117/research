"""research-bilingual-qa：对照页硬指标 + 可选 LLM 抽验。只读，不改库。

    docker compose exec app research-bilingual-qa --article-id 21118
    docker compose exec app research-bilingual-qa --source stratechery --limit 20
    docker compose exec app research-bilingual-qa --article-id 21118 --llm --llm-sample 1
    docker compose exec app research-bilingual-qa --limit 50 --json --report /data/status/bilingual-qa.json
    docker compose exec app research-bilingual-qa --omissions --source stratechery --limit 50
    docker compose exec app research-bilingual-qa --omissions --from-inventory legacy-structural --limit 50

退出码：0 通过（或有警告但没加 --strict），1 有失败项（硬指标，或 --llm 时模型给 fail；
加 --strict 时警告也算），2 参数、库或 LLM 凭证的问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from researchlib.config import Settings
from researchlib.db import connect
from researchlib.llm import LLMError, LLMUnavailable, ensure_llm
from researchlib.translate import inventory, omission, qa

DEFAULT_LIMIT = 20


def _human_line(report: qa.BilingualReport) -> str:
    status = report.status.upper()
    parts = [
        f"#{report.article_id}",
        report.source_id,
        status,
        f"pairs={report.pairs}",
        f"leaves={report.en_leaves}/{report.zh_leaves}",
        f"display={report.en_blocks}/{report.zh_blocks}",
        f"gap={report.display_gap}",
        f"maxEN={report.max_en_chars}",
        f"giant={len(report.giant)}",
        f"dup={len(report.near_duplicates)}",
        f"empty={len(report.long_empty_zh)}",
    ]
    if report.issues:
        parts.append("issues=" + ",".join(issue.code for issue in report.issues))
    return " ".join(parts)


def _summary(reports: list[qa.BilingualReport], skipped: int) -> dict[str, Any]:
    codes: dict[str, int] = {}
    for report in reports:
        for issue in report.issues:
            codes[issue.code] = codes.get(issue.code, 0) + 1
    llm = [report.llm for report in reports if report.llm]
    return {
        "checked": len(reports),
        "pass": sum(1 for report in reports if report.status == "pass"),
        "warn": sum(1 for report in reports if report.status == "warn"),
        "fail": sum(1 for report in reports if report.status == "fail"),
        "skipped": skipped,
        "issues": dict(sorted(codes.items())),
        "llm": {
            "checked": len(llm),
            "pass": sum(1 for item in llm if item.get("verdict") == "pass"),
            "warn": sum(1 for item in llm if item.get("verdict") == "warn"),
            "fail": sum(1 for item in llm if item.get("verdict") == "fail"),
        },
    }


def _print_report(report: qa.BilingualReport) -> None:
    print(_human_line(report))
    for issue in report.issues:
        print(f"  {issue.level}: {issue.code} ×{issue.count} — {issue.detail}")
    if report.llm:
        item = report.llm
        print(f"  llm {item.get('verdict')} {item.get('score')} {item.get('summary') or ''}".rstrip())
        for issue in item.get("issues") or []:
            print(f"    - [{issue.get('index')}] {issue.get('kind')}: {issue.get('note')}")


def _spread(ids: list[int], limit: int) -> list[int]:
    """从清单里均匀地挑 limit 篇。清单按发布时间新的在前，这样新旧都抽得到；同一份清单每次挑的都一样。"""
    if limit >= len(ids):
        return list(ids)
    step = len(ids) / limit
    return [ids[int(index * step)] for index in range(limit)]


def _omission_lines(result: omission.ArticleOmissions, pairs: dict[int, omission.Pair]) -> list[str]:
    extra = f" 只有英文 {result.english_only} 对" if result.english_only else ""
    if result.skipped:
        return [f"#{result.article_id} 跳过：{result.skipped}{extra}"]
    head = f"#{result.article_id} pairs={result.pairs} flagged={len(result.findings)}"
    if result.basis == "page":
        head += " 按页面配对" + extra
    if result.unverified:
        head += f" unverified={result.unverified}"
    if result.failed_batches:
        head += f" failed_batches={result.failed_batches}"
    lines = [head]
    for ordinal, quotes in sorted(result.findings.items()):
        lines.extend(f"  [{ordinal}] 漏：{quote}" for quote in quotes)
        zh = pairs[ordinal].zh if ordinal in pairs else ""
        lines.append(f"       中文：{zh[:160]}{'…' if len(zh) > 160 else ''}")
    lines.extend(f"  摘句在原文里找不到、丢掉：{quote}" for quote in result.dropped)
    lines.extend(f"  这一批没核：{reason}" for reason in result.failures)
    return lines


def _omissions(args: argparse.Namespace, settings: Settings) -> int:
    """段内漏句抽查。只读：读回执（没回执的旧稿读页面）、问模型、打印；不写库，不改回执。"""
    listed: list[int] = []
    generated = ""
    if args.from_inventory:
        try:
            listed, generated = inventory.listed(settings.data_dir, args.from_inventory)
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 2
    try:
        ensure_llm(settings)
    except LLMUnavailable as exc:
        print(f"LLM 不可用：{exc}", file=sys.stderr)
        return 2
    conn = connect(settings)
    results: list[omission.ArticleOmissions] = []
    details: list[dict[str, Any]] = []
    usage = omission.Usage()
    exit_code = 0
    try:
        if args.article_id:
            ids = list(dict.fromkeys(args.article_id))
        elif args.from_inventory:
            ids = _spread(listed, max(1, args.limit))
            print(
                f"盘点清单（{generated or '时间不详'}）里 {args.from_inventory} 共 {len(listed)} 篇，"
                f"均匀抽 {len(ids)} 篇",
                file=sys.stderr, flush=True,
            )
        else:
            ids = qa.translated_article_ids(conn, limit=max(1, args.limit), source_id=args.source or None)
        for article_id in ids:
            receipt = omission.load_receipt(conn, article_id)
            pairs = omission.receipt_pairs(receipt)
            basis, english_only = "receipt", 0
            if not pairs and not (isinstance(receipt, dict) and receipt.get("segments")):
                # 没回执的旧稿：按页面上并排显示的段对核。页面分开展示的没有段对，跳过。
                basis = "page"
                try:
                    pairs, english_only = omission.page_pairs(qa.pairs_for_review(conn, settings, article_id))
                except qa.QaError:
                    pairs = []
            by_ordinal = {pair.ordinal: pair for pair in pairs}
            if not pairs:
                why = "没回执，页面也没有并排的段对" if basis == "page" else "回执里没有可核的段"
                result = omission.ArticleOmissions(
                    article_id, skipped=why, basis=basis, english_only=english_only,
                )
            else:
                try:
                    result = omission.review_pairs(article_id, pairs, data_dir=settings.data_dir)
                except (RuntimeError, ValueError) as exc:
                    print(f"#{article_id} 抽查停下：{exc}", file=sys.stderr)
                    exit_code = 2
                    break
                result.basis, result.english_only = basis, english_only
            results.append(result)
            details.append(result.as_dict(by_ordinal))
            usage.merge(result.usage)
            if not args.json:
                for line in _omission_lines(result, by_ordinal):
                    print(line, flush=True)
    finally:
        conn.close()

    reviewed = [result for result in results if not result.skipped]
    checked = sum(result.checked for result in reviewed)
    flagged = sum(len(result.findings) for result in reviewed)
    summary = {
        "articles": len(reviewed),
        "skipped": len(results) - len(reviewed),
        "pairs": checked,
        "flagged": flagged,
        "rate": round(flagged / checked, 4) if checked else 0.0,
        "unverified": sum(result.unverified for result in reviewed),
        "failedBatches": sum(result.failed_batches for result in reviewed),
        "byPage": sum(result.basis == "page" for result in reviewed),
        "englishOnly": sum(result.english_only for result in results),
        "usage": usage.as_dict(),
    }
    payload = {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "articles": details,
        "summary": summary,
        "exitCode": exit_code,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        rate = f"{summary['rate'] * 100:.1f}%"
        print(
            f"核对 {summary['articles']} 篇 {checked} 段：{flagged} 段疑似漏句（{rate}）"
            + (f"；模型摘的原文找不到、丢掉 {summary['unverified']} 条" if summary["unverified"] else "")
            + (f"；{summary['failedBatches']} 批回答用不了、没核" if summary["failedBatches"] else "")
            + (f"；{summary['skipped']} 篇跳过" if summary["skipped"] else "")
        )
        if summary["byPage"] or summary["englishOnly"]:
            print(
                f"其中 {summary['byPage']} 篇没回执，按页面上并排的段对核"
                + (f"；页面上只有英文、没有中文的 {summary['englishOnly']} 对（整段没译，没送模型）"
                   if summary["englishOnly"] else "")
            )
        print(
            f"用量：请求 {usage.requests} 次，输入 {usage.prompt:,} token（缓存命中 {usage.cache_hit:,}），"
            f"输出 {usage.completion:,} token（思考 {usage.reasoning:,}）"
        )
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对照页质量检查：硬指标 + 可选 LLM 抽验（只读）")
    parser.add_argument("--article-id", type=int, action="append", default=[], help="指定文章 id，可重复")
    parser.add_argument("--source", default="", help="不点名时只扫该来源")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"不点名时按译文更新时间取最近几篇（默认 {DEFAULT_LIMIT}）",
    )
    parser.add_argument("--json", action="store_true", help="按一个 JSON 对象输出")
    parser.add_argument("--report", default="", help="把结果另写成 JSON 文件")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="对硬指标没有失败的篇再跑 LLM 抽验（调站内 DeepSeek，花钱）",
    )
    parser.add_argument("--llm-sample", type=int, default=3, help="最多抽验几篇（默认 3）")
    parser.add_argument("--strict", action="store_true", help="有警告也返回 1")
    parser.add_argument(
        "--omissions",
        action="store_true",
        help="段内漏句抽查：按回执逐段问模型译文漏了什么（调站内 DeepSeek，花钱；不跑硬指标，不拦入库）",
    )
    parser.add_argument(
        "--from-inventory",
        default="",
        metavar="类别",
        help="和 --omissions 一起用：从盘点清单（research-translate-inventory）的这一类里均匀抽 --limit 篇，"
        "比如没回执的旧稿 legacy-structural",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.from_inventory and not args.omissions:
        print("--from-inventory 只和 --omissions 一起用", file=sys.stderr)
        return 2
    if args.from_inventory and (args.article_id or args.source):
        print("--from-inventory 不和 --article-id、--source 一起用", file=sys.stderr)
        return 2
    if args.omissions:
        return _omissions(args, settings)
    if args.llm:
        try:
            ensure_llm(settings)
        except LLMUnavailable as exc:
            print(f"LLM 不可用：{exc}", file=sys.stderr)
            return 2
    conn = connect(settings)
    reports: list[qa.BilingualReport] = []
    skipped = 0
    try:
        if args.article_id:
            ids = list(dict.fromkeys(args.article_id))
        else:
            ids = qa.translated_article_ids(
                conn, limit=max(1, args.limit), source_id=args.source or None
            )
        if not ids:
            print("没有找到有译文的文章。", file=sys.stderr)
            return 0
        for article_id in ids:
            try:
                report = qa.check_article(conn, settings, article_id)
            except qa.QaError as exc:
                print(f"#{article_id} {exc}", file=sys.stderr)
                return 2
            if report is None:
                skipped += 1
                print(f"#{article_id} 没有译文，跳过", file=sys.stderr)
                continue
            reports.append(report)
    finally:
        conn.close()

    llm_error = False
    if args.llm:
        reviewed = 0
        llm_conn = connect(settings)
        try:
            for report in reports:
                if reviewed >= max(0, args.llm_sample):
                    break
                if report.status == "fail":
                    # 硬指标已经点名了，先把钱花在没有失败项的篇上。
                    continue
                pairs = qa.pairs_for_review(llm_conn, settings, report.article_id)
                try:
                    report.llm = qa.review_with_llm(settings, report, pairs)
                except (LLMUnavailable, LLMError, qa.QaError) as exc:
                    print(f"#{report.article_id} LLM 抽验失败：{exc}", file=sys.stderr)
                    llm_error = True
                    break
                reviewed += 1
        finally:
            llm_conn.close()

    summary = _summary(reports, skipped)
    llm_fail = any(report.llm and report.llm.get("verdict") == "fail" for report in reports)
    llm_warn = any(report.llm and report.llm.get("verdict") == "warn" for report in reports)
    exit_code = 1 if summary["fail"] or llm_fail or (args.strict and (summary["warn"] or llm_warn)) else 0
    if llm_error:
        exit_code = 2
    payload = {
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "articles": [report.as_dict() for report in reports],
        "summary": summary,
        "exitCode": exit_code,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            _print_report(report)
        print(
            f"检查 {summary['checked']} 篇：pass {summary['pass']}、"
            f"warn {summary['warn']}、fail {summary['fail']}"
            + (f"，{skipped} 篇没有译文" if skipped else "")
        )
        if summary["issues"]:
            detail = "、".join(f"{code} {count} 篇" for code, count in summary["issues"].items())
            print(f"问题：{detail}")
        if summary["llm"]["checked"]:
            llm = summary["llm"]
            print(f"LLM 抽验 {llm['checked']} 篇：pass {llm['pass']}、warn {llm['warn']}、fail {llm['fail']}")
        if exit_code == 1:
            reasons: list[str] = []
            if summary["fail"]:
                reasons.append(f"硬指标 {summary['fail']} 篇")
            if llm_fail:
                reasons.append("LLM 抽验 fail")
            label = "有失败项" if reasons else "有警告（--strict）"
            print(f"{label}{'：' + '、'.join(reasons) if reasons else ''}。", file=sys.stderr)

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""research-bilingual-qa：对照页硬指标 + 可选 LLM 抽验。只读，不改库。

    docker compose exec app research-bilingual-qa --article-id 21118
    docker compose exec app research-bilingual-qa --source stratechery --limit 20
    docker compose exec app research-bilingual-qa --article-id 21118 --llm --llm-sample 1
    docker compose exec app research-bilingual-qa --limit 50 --json --report /data/status/bilingual-qa.json

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
from researchlib.translate import qa

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
    args = parser.parse_args(argv)

    settings = Settings.from_env()
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

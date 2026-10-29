"""python -m researchlib.relevance：显式初始化、导入、校验、导出与本机服务。"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from researchlib.relevance.demo import demo_records
from researchlib.relevance.schema import Manifest, canonical
from researchlib.relevance.store import Store, read_jsonl, validate_records


def write_private(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 不覆盖已有导出，避免覆盖历史标签；原文/标签导出默认仅当前用户可读。
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def report_coverage(dataset):
    coverage = dataset.report()
    print(canonical(coverage), file=sys.stderr)
    for warning in coverage["warnings"]:
        print("告警：" + warning["message"], file=sys.stderr)
    return coverage


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立本机原文相关性标注，不连接业务库、不自动调用模型")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "demo", "import", "export", "serve", "report"):
        command = commands.add_parser(name)
        command.add_argument(
            "--db", type=Path, required=True, help="独立 SQLite 路径，建议放在被忽略的 data/relevance/"
        )
        if name == "init":
            command.add_argument("--dataset-id", required=True)
            command.add_argument("--seed", required=True)
        elif name == "import":
            command.add_argument("--input", type=Path, required=True)
        elif name == "export":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--source", choices=("human", "ai_weak", "all"), default="human")
        elif name == "serve":
            command.add_argument("--annotator-id", required=True, help="操作员指定的本机标注身份，不是登录认证")
            command.add_argument("--port", type=int, default=8099)
            command.add_argument("--provider", help="显式选配 module:function，不配就只能标导入任务")
    check = commands.add_parser("check")
    check.add_argument("input", type=Path)
    fixture = commands.add_parser("fixture")
    fixture.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            Store.initialize(args.db, Manifest(dataset_id=args.dataset_id, seed=args.seed))
        elif args.command in {"demo", "import"}:
            rows = demo_records() if args.command == "demo" else read_jsonl(args.input)
            dataset = validate_records(rows)
            store = Store(args.db) if args.db.exists() else Store.initialize(args.db, dataset.manifest)
            store.import_records(rows)
            print(f"已导入 {len(dataset.tasks)} 个冻结任务；未生成标签。")
            report_coverage(store.snapshot())
        elif args.command == "check":
            dataset = validate_records(read_jsonl(args.input))
            human = sum(e.source.kind == "human" for e in dataset.events)
            print(
                f"校验通过：{len(dataset.tasks)} 任务，human {human} 事件，AI 弱标 {len(dataset.events) - human} 事件。"
            )
            report_coverage(dataset)
        elif args.command == "fixture":
            write_private(args.output, "".join(canonical(r.model_dump(mode="json")) + "\n" for r in demo_records()))
        elif args.command == "export":
            store = Store(args.db)
            content = store.export(args.source)
            # 报告针对本次实际导出内容；弱标导出也不能显示全库 human 的覆盖率。
            from researchlib.relevance.store import Record

            dataset = validate_records([Record.model_validate_json(line) for line in content.splitlines()])
            coverage = report_coverage(dataset)
            report_path = args.output.with_name(args.output.name + ".report.json")
            if args.output.exists() or report_path.exists():
                raise FileExistsError("导出或配套覆盖报告已存在；请另选文件名")
            write_private(args.output, content)
            write_private(report_path, canonical(coverage) + "\n")
        elif args.command == "report":
            report_coverage(Store(args.db).snapshot())
        elif args.command == "serve":
            import uvicorn

            from researchlib.web.relevance import create_annotation_app

            Store(args.db).snapshot()  # 启动前只读检查，不建表、不补数据。
            provider = None
            if args.provider:
                module, name = args.provider.split(":", 1)
                provider = getattr(importlib.import_module(module), name)
                if not callable(provider):
                    raise ValueError("provider 必须可调用")
            app = create_annotation_app(args.db, args.annotator_id, candidate_provider=provider)
            print(f"本机标注入口：http://127.0.0.1:{args.port}/annotate；身份 {args.annotator_id}")
            # 反向代理头不能把外部访客伪装成回环地址。
            uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"未执行完成：{exc}\n")


if __name__ == "__main__":
    main()

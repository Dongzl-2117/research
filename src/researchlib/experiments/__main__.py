"""python -m researchlib.experiments：显式冻结、初始化、服务与结果导出。"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from pydantic import TypeAdapter

from researchlib.experiments.schema import QuerySpec, Snapshot, canonical
from researchlib.experiments.store import Store
from researchlib.relevance.__main__ import write_private


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立本机算法对照，不连接生产或自动调用模型")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo")
    demo.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--dsn-file", type=Path, required=True, help="明确的本机离线实验库连接文件，不读取 .env")
    capture.add_argument("--queries", type=Path, required=True)
    capture.add_argument("--sources", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    for name in ("init", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--snapshot", type=Path, required=True)
        command.add_argument("--db", type=Path, required=True)
        if name == "serve":
            command.add_argument("--port", type=int, default=8100)
            command.add_argument("--sources", type=Path, help="私有快照必填；来源策略变更时拒绝启动")
            command.add_argument("--generator", help="显式选配 module:function；默认无任何模型入口")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from researchlib.experiments.demo import demo_snapshot
            write_private(args.output, canonical(demo_snapshot().model_dump(mode="json")) + "\n")
            print("已保存合成快照，不是真实资料。")
        elif args.command == "capture":
            from researchlib.experiments.capture import capture
            specs = TypeAdapter(list[QuerySpec]).validate_json(args.queries.read_text())
            if args.output.exists():
                raise FileExistsError("快照已存在，请换一个文件名")
            snapshot = capture(args.dsn_file.read_text().strip(), args.sources, specs)
            write_private(args.output, canonical(snapshot.model_dump(mode="json")) + "\n")
            print(f"已冻结 {len(snapshot.cases)} 个问题；只读事务已回滚，未调用模型。")
        else:
            snapshot = Snapshot.model_validate_json(args.snapshot.read_text())
            if args.command == "init":
                Store.initialize(args.db, snapshot.fingerprint)
                print("已创建独立实验库，未连接主站。")
            else:
                import uvicorn

                from researchlib.web.experiments import create_experiment_app
                generator = None
                if args.generator:
                    module, name = args.generator.split(":", 1)
                    generator = getattr(importlib.import_module(module), name)
                    if not callable(generator):
                        raise ValueError("generator 必须可调用")
                app = create_experiment_app(snapshot, args.db, sources_path=args.sources,
                                            generator=generator, generator_label=args.generator)
                print(f"本机算法实验台：http://127.0.0.1:{args.port}/experiments")
                uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"未执行完成：{exc}\n")


if __name__ == "__main__":
    main()

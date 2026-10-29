"""显式离线训练：输入为 PR115 导出，只用冻结 train，不生成上线批准文件。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from researchlib.ranking.contracts import digest
from researchlib.ranking.evaluation import evaluate_pairs
from researchlib.ranking.linear import LinearRanker, fit
from researchlib.ranking.relevance_adapter import read_preferences


def write_private(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def train_and_evaluate(data, *, allow_synthetic=False, ai_weight=0, seed=0, epochs=200):
    """固定参数的基线；验证/测试只评估一次，不按其成绩重新训练或挑权重。"""
    model = fit(data.examples("train"), allow_synthetic=allow_synthetic, ai_weight=ai_weight,
                seed=seed, epochs=epochs)
    reports = {split: evaluate_pairs(model, data.examples(split)) for split in ("validation", "test")}
    report = {
        "manifest_id": data.manifest_id, "model_id": model.model_id, "model_revision": model.model_revision,
        "training": model.to_record()["training_summary"], "coverage": data.report, "evaluation": reports,
        "training_provenance": training_provenance(data),
        "activation": "disabled; 此命令不生成review或修改应用配置",
        "limitations": ["CPU词法交叉特征baseline不是双语语义模型；合成或AI弱标不能代表真实泛化。",
                        "directional accuracy不是P@5；没有逐候选相关金标，不编造搜索精度。",
                        "pairwise相对分数没有校准的无答案拒答阈值；neither单独报告。",
                        "按实体/主题/文档族的连通闭包可能使划分不足；不会自动拆组或换seed。"],
    }
    return model, report


def training_provenance(data):
    pairs = sorted((p for p in data.pairs if p.split == "train"), key=lambda p: (p.task_id, p.event_id))
    snapshots = [{"task_id": p.task_id, "group_id": p.group_id, "event_id": p.event_id,
                  "revision": p.revision, "query": p.example.query.to_record(),
                  "a": p.example.a.to_record(), "b": p.example.b.to_record(),
                  "choice": p.example.choice, "source": p.example.source, "material": p.example.material}
                 for p in pairs]
    return {"manifest_id": data.manifest_id, "partition": "train", "snapshot_hash": digest(snapshots),
            "task_ids": sorted({p.task_id for p in pairs}), "group_ids": sorted({p.group_id for p in pairs})}


def verify_training_record(model, data, report):
    if (not isinstance(report, dict) or report.get("model_revision") != model.model_revision
            or report.get("manifest_id") != data.manifest_id
            or report.get("training_provenance") != training_provenance(data)
            or report.get("training") != model.summary.to_record()):
        raise ValueError("缺少与权重/冻结train快照一致的训练记录；不能把未知训练来源模型称作独立holdout评测")


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线偏好学习，不连接业务库或调用模型服务")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--input", type=Path, required=True)
    train = commands.add_parser("train")
    train.add_argument("--input", type=Path, required=True)
    train.add_argument("--output-model", type=Path, required=True)
    train.add_argument("--report", type=Path, required=True)
    train.add_argument("--allow-synthetic", action="store_true")
    train.add_argument("--ai-weight", type=float, default=0)
    train.add_argument("--seed", type=int, default=0)
    train.add_argument("--epochs", type=int, default=200)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--input", type=Path, required=True)
    evaluate.add_argument("--model", type=Path, required=True)
    evaluate.add_argument("--training-report", type=Path, required=True)
    evaluate.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    data = read_preferences(args.input)
    if args.command == "check":
        print(json.dumps(data.report, ensure_ascii=False, indent=2))
    elif args.command == "train":
        if args.output_model.resolve() == args.report.resolve() or args.output_model.exists() or args.report.exists():
            raise FileExistsError("模型/报告路径重复或已存在，不覆盖历史产物")
        model, report = train_and_evaluate(data, allow_synthetic=args.allow_synthetic, ai_weight=args.ai_weight,
                                           seed=args.seed, epochs=args.epochs)
        write_private(args.output_model, model.to_json() + "\n")
        write_private(args.report, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        print(json.dumps({"model_revision": model.model_revision, "manifest_id": data.manifest_id,
                          "activation": "disabled"}, ensure_ascii=False))
    else:
        model = LinearRanker.from_json(args.model.read_text(encoding="utf-8"))
        verify_training_record(model, data, json.loads(args.training_report.read_text(encoding="utf-8")))
        report = {"manifest_id": data.manifest_id, "model_revision": model.model_revision, "coverage": data.report,
                  "evaluation": {s: evaluate_pairs(model, data.examples(s)) for s in ("validation", "test")}}
        write_private(args.report, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()

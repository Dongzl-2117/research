"""隔离核验。不写库，不部署，不依赖测试文件。

样本在同目录 semantic_samples.json。没有 DEEPSEEK_API_KEY 时退出，不算实跑。
有 key 时只调独立核验。每例开始和结束都落盘、刷屏，单条网络失败记为未测并继续。
完整回执逐例写进 semantic-sample.jsonl，并在每例结束后原子更新 semantic-sample.json。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from researchlib.llm_provider import deepseek_api_key
from researchlib.translate.harness import evidence_id, verify_facts
from researchlib.translate.llm import LLMTransportError

HERE = Path(__file__).resolve().parent
_FIELDS = ("id", "expect", "status", "reason", "sent_evidence_id", "evidence_id")


def _network(exc: Exception) -> bool:
    if isinstance(exc, (LLMTransportError, TimeoutError, ConnectionError, OSError)):
        return True
    if type(exc).__name__ in {"URLError", "HTTPError", "IncompleteRead", "RemoteDisconnected"}:
        return True
    # DeepSeek 的 500/502/503 是服务端临时错误，不能算核验结论。
    return re.search(r"\bHTTP 5\d\d\b", str(exc)) is not None


def _write_json(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _run_one(item: dict, data_dir: Path) -> dict:
    source, target = item["source"], item["target"]
    sent = evidence_id(source, target)
    try:
        found, raw = verify_facts(source, target, data_dir)
    except Exception as exc:  # noqa: BLE001 — 单条失败不拖垮其余样本
        return {
            "id": item["id"],
            "expect": item["expect"],
            "status": "untested" if _network(exc) else "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "sent_evidence_id": sent,
            "evidence_id": None,
            "verdict": None,
            "claims": None,
            "raw": None,
        }
    report = raw if isinstance(raw, dict) else {"raw": raw}
    return {
        "id": item["id"],
        "expect": item["expect"],
        "status": found.status,
        "reason": found.reason,
        "sent_evidence_id": sent,
        "evidence_id": report.get("evidence_id"),
        "verdict": report.get("verdict"),
        "claims": report.get("claims"),
        "raw": report,
    }


def main() -> int:
    samples = json.loads((HERE / "semantic_samples.json").read_text(encoding="utf-8"))
    if not deepseek_api_key():
        print("未实跑：没有 DEEPSEEK_API_KEY。没有调模型。")
        for item in samples:
            print(f"{item['expect']} {item['id']}")
        return 2
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/translate-semantic-sample")
    data_dir.mkdir(parents=True, exist_ok=True)
    progress = data_dir / "semantic-sample.jsonl"
    out = data_dir / "semantic-sample.json"
    progress.write_text("", encoding="utf-8")
    rows: list[dict] = []
    for number, item in enumerate(samples, 1):
        start = {"event": "start", "number": number, "total": len(samples), "id": item["id"],
                 "expect": item["expect"]}
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(start, ensure_ascii=False) + "\n")
            handle.flush()
        print(json.dumps(start, ensure_ascii=False), flush=True)
        row = _run_one(item, data_dir)
        rows.append(row)
        brief = {key: row[key] for key in _FIELDS}
        end = {"event": "end", **brief}
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
        _write_json(out, rows)
        print(json.dumps(end, ensure_ascii=False), flush=True)
    print(out)
    if any(row["status"] not in {row["expect"], "untested"} for row in rows):
        return 1
    if any(row["status"] == "untested" for row in rows):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

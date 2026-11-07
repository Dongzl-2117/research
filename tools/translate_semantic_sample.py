"""隔离核验。不写库，不部署，不依赖测试文件。

样本在同目录 semantic_samples.json。没有 DEEPSEEK_API_KEY 时退出，不算实跑。
有 key 时只调独立核验。单条网络失败记为未测并继续。完整回执写入 semantic-sample.json。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from researchlib.llm_provider import deepseek_api_key
from researchlib.translate.harness import evidence_id, verify_facts
from researchlib.translate.llm import LLMTransportError

HERE = Path(__file__).resolve().parent


def _network(exc: Exception) -> bool:
    if isinstance(exc, (LLMTransportError, TimeoutError, ConnectionError, OSError)):
        return True
    return type(exc).__name__ in {"URLError", "HTTPError", "IncompleteRead", "RemoteDisconnected"}


def _run_one(item: dict, data_dir: Path) -> dict:
    source, target = item["source"], item["target"]
    sent = evidence_id(source, target)
    try:
        found, raw = verify_facts(source, target, data_dir)
    except Exception as exc:
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
    rows = [_run_one(item, data_dir) for item in samples]
    out = data_dir / "semantic-sample.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)
    for row in rows:
        brief = {key: row[key] for key in ("id", "expect", "status", "reason", "sent_evidence_id", "evidence_id")}
        print(json.dumps(brief, ensure_ascii=False))
    if any(row["status"] not in {row["expect"], "untested"} for row in rows):
        return 1
    if any(row["status"] == "untested" for row in rows):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""临时组合 PR99/PR115，运行真实 schema/Store/HTTP 导出，不复制或合并 UI 模块。

所有输入是本仓库原创合成材料。输出默认不覆盖，必须显式给隔离目录；可选 Postgres
检查只接受本机 research_eval_* 库，且要求文章表为空，不读取默认数据库。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def compose(relevance_root):
    import researchlib
    import researchlib.web
    base = relevance_root.resolve() / "src" / "researchlib"
    if not (base / "relevance" / "store.py").is_file() or not (base / "web" / "relevance.py").is_file():
        raise ValueError("指定worktree不含PR115 schema与UI；未下载或替换依赖")
    researchlib.__path__.append(str(base))
    researchlib.web.__path__.append(str(base / "web"))


def check_adapter_revisions(output, tasks):
    """模拟human类别只做协议断言，单独SQLite，不进入训练/评估的金标。"""
    from researchlib.relevance.schema import LabelRequest, LabelSource, Manifest
    from researchlib.relevance.store import Record, Store

    from researchlib.ranking.relevance_adapter import adapt_records
    store = Store.initialize(output / "adapter-protocol-only.sqlite3",
                             Manifest(dataset_id="simulated-label-protocol-only", seed="fixed-protocol-v1"))
    store.add_tasks(tasks[:2])
    human = LabelSource(kind="human", labeler_id="simulated-human-protocol-fixture")
    weak = LabelSource(kind="ai_weak", labeler_id="simulated-ai-protocol-fixture", model_name="not-a-real-model")
    for index, task in enumerate(tasks[:2]):
        for actor in (human, weak):
            presentation = store.present(task.task_id, actor.labeler_id)
            store.label(LabelRequest(presentation_id=presentation.presentation_id, decision="left",
                                     expected_revision=0, request_id=f"initial-{index}-{actor.kind}"), actor)
            if actor.kind == "human":
                store.label(LabelRequest(presentation_id=presentation.presentation_id,
                                         decision="withdraw" if index == 0 else "right",
                                         expected_revision=1, request_id=f"changed-{index}"), actor)
    data = adapt_records([Record.model_validate_json(line) for line in store.export("all").splitlines()])
    assert data.report["latest_sources"] == {"human": 2, "ai_weak": 2}
    assert len(data.pairs) == 3
    gold = [p for p in data.pairs if p.example.source == "user"]
    assert len(gold) == 1 and gold[0].task_id == tasks[1].task_id and gold[0].revision == 2
    p = store.present(tasks[1].task_id, human.labeler_id)
    winner = gold[0].example.a if gold[0].example.choice == "a_better" else gold[0].example.b
    assert winner.candidate_id == p.right_id
    assert len([p for p in data.pairs if p.example.source == "ai"]) == 2
    return "passed; 模拟human类别仅协议测试，未混入学习或金标评测"


def check_provider(database_url, output):
    from urllib.parse import urlsplit

    import psycopg
    from psycopg.rows import dict_row
    from researchlib.relevance.schema import Manifest
    from researchlib.relevance.store import Store

    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.ingest.model import ParsedDoc
    from researchlib.ingest.store import store_documents
    from researchlib.ranking.relevance_adapter import build_library_provider
    from researchlib.retrieval import reset_stats_cache
    from researchlib.sources import enabled_sources, load_sources

    parsed = urlsplit(database_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.path[1:].startswith("research_eval_"):
        raise ValueError("provider复现只允许显式本机research_eval_*隔离库")
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        exists = conn.execute("SELECT to_regclass('articles') AS name").fetchone()["name"]
        if exists and conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]:
            raise ValueError("隔离库已有文章，拒绝清空或复用；请创建新的空eval库")
    settings = Settings.from_env({"DATABASE_URL": database_url, "DATA_DIR": str(output / "ingest"),
                                  "SOURCES_FILE": str(ROOT / "config" / "sources.yaml")})
    sources = enabled_sources(load_sources(settings.sources_path))
    source = next(s for s in sources if s.id == "fangwei-mp")
    with prepare(settings) as conn:
        from datetime import UTC, datetime
        store_documents(conn, source=source, data_dir=settings.data_dir, archive_files=3,
                        images_in_archive=0, errors=[], docs=[
                            ParsedDoc("ranking-ui-a", "合成设备札记甲", datetime(2026, 1, 1, tzinfo=UTC),
                                      "ASML维护合同按年收费，设备投入使用以后保养服务带来续签款项。", {}, []),
                            ParsedDoc("ranking-ui-b", "合成设备札记乙", datetime(2026, 1, 1, tzinfo=UTC),
                                      "阿斯麦向已交付机器提供升级配件，升级订单与新机销售分开形成收入。", {}, []),
                            ParsedDoc("ranking-ui-c", "合成其他公司", datetime(2026, 1, 1, tzinfo=UTC),
                                      "Walmart维护合同用于配送车辆，费用只属于沃尔玛。", {}, []),
                        ])
    reset_stats_cache()
    connections = []

    def fresh():
        conn = psycopg.connect(database_url, row_factory=dict_row)
        connections.append(conn)
        return conn

    # 模拟private_corpus分类仅验证UI generate协议；此临时库不进入学习/真实人工评测。
    provider = build_library_provider(connection_factory=fresh, sources=sources,
                                      public_base_url="https://example.invalid",
                                      topic_resolver=lambda **kw: ["fixture:equipment-service"],
                                      material="private_corpus")
    from fastapi.testclient import TestClient
    from researchlib.web.relevance import create_annotation_app
    db = output / "provider-ui.sqlite3"
    Store.initialize(db, Manifest(dataset_id="isolated-provider-contract-only", seed="provider-v1"))
    app = create_annotation_app(db, "simulated-contract-operator", candidate_provider=provider)
    with TestClient(app, base_url="http://127.0.0.1:8099", client=("127.0.0.1", 52001)) as client:
        response = client.post("/api/relevance/generate", json={
            "query": "ASML 售后收入", "scope": {"source_ids": [source.id]},
        }, headers={"x-relevance-csrf": app.state.relevance_csrf})
        assert response.status_code == 200, response.text
        state = client.get("/api/relevance/state").json()
        pair = state["current"]
        assert pair and {pair[side]["source_id"] for side in ("left", "right")} == {source.id}
        assert all("Walmart" not in pair[side]["text"] for side in ("left", "right"))
        assert all(pair[side]["content_version"] and pair[side]["content_hash"] for side in ("left", "right"))
        assert all(pair[side]["source_url"].startswith("https://example.invalid/articles/")
                   for side in ("left", "right"))
    assert connections and all(c.closed and c.read_only for c in connections)
    try:
        provider(query="ASML 售后收入", scope={"source_ids": ["not-allowed"]})
    except ValueError:
        pass
    else:
        raise AssertionError("未知来源未被拒绝")
    return {"status": "passed", "material": "synthetic; private_corpus分类仅测generate协议，未训练/未标金标",
            "connections": len(connections), "read_only": True, "semantic_calls": 0}


def run(output, database_url=None):
    from fastapi.testclient import TestClient
    from researchlib.relevance.schema import Algorithm, Candidate, LabelRequest, LabelSource, Manifest, Task
    from researchlib.relevance.store import Record, Store
    from researchlib.web.relevance import create_annotation_app

    from researchlib.ranking.__main__ import train_and_evaluate, verify_training_record, write_private
    from researchlib.ranking.relevance_adapter import adapt_records, read_preferences
    from researchlib.ranking.runtime import load_reviewed_ranker
    fixture = json.loads((ROOT / "tools/ranking/fixtures/pairs.json").read_text(encoding="utf-8"))
    store = Store.initialize(output / "labels.sqlite3",
                             Manifest(dataset_id="synthetic-ranking-loop-v1", seed=fixture["seed"]))
    tasks, expected, decisions = [], {}, {}
    for index, (query, positive, negative, decision) in enumerate(fixture["examples"]):
        candidates = tuple(Candidate(
            source_id="original-synthetic-ranking-fixture", article_id=f"fixture-{index}-{side}", chunk_id="0",
            window_id="window-v1", document_family_id=f"fictional-family-{index}-{side}",
            content_version="authored-synthetic-v1", article_title=f"原创虚构札记{index}-{side}", text=text,
            source_url=f"https://example.invalid/fixture/{index}/{side}",
            algorithm=Algorithm(name="authored-synthetic-pair", version="1"),
        ) for side, text in enumerate((positive, negative)))
        task = Task(query=query, entity_ids=[f"fictional-entity:{index}"], topic_ids=[f"fictional-topic:{index}"],
                    candidates=candidates, material="synthetic")
        tasks.append(task)
        expected[task.task_id] = candidates[0].candidate_id
        decisions[task.task_id] = decision
    store.add_tasks(tasks)
    actor = LabelSource(kind="ai_weak", labeler_id="simulated-weak-fixture", model_name="deterministic-fixture",
                        model_version="1", prompt_version="not-an-llm-simulation-v1")
    for index, task in enumerate(tasks):
        p = store.present(task.task_id, actor.labeler_id)
        correct = "left" if p.left_id == expected[task.task_id] else "right"
        decision = decisions[task.task_id]
        first = ("right" if correct == "left" else "left") if index == 0 else (
            correct if decision in {"prefer", "withdraw"} else decision)
        store.label(LabelRequest(presentation_id=p.presentation_id, decision=first,
                                 expected_revision=0, request_id=f"initial-{index}"), actor)
        if index == 0 or decision == "withdraw":
            store.label(LabelRequest(presentation_id=p.presentation_id,
                                     decision="withdraw" if decision == "withdraw" else correct,
                                     expected_revision=1, request_id=f"revision-{index}"), actor)
    app = create_annotation_app(output / "labels.sqlite3", "unused-human-operator")
    with TestClient(app, base_url="http://127.0.0.1:8099", client=("127.0.0.1", 52001)) as client:
        human_export = client.get("/api/relevance/export")
        assert human_export.status_code == 200
        assert not any(json.loads(line)["record_type"] == "label_event" for line in human_export.text.splitlines())
        response = client.get("/api/relevance/export", params={"source": "all"})
        assert response.status_code == 200
        exported = response.text
    path = output / "ui-export.jsonl"
    write_private(path, exported)
    data = read_preferences(path)
    assert data.report["latest_sources"] == {"ai_weak": len(tasks)}
    assert data.report["latest_decisions"]["withdraw"] == 1
    assert len(data.pairs) == len(tasks) - 1
    assert not any(p.example.source == "user" for p in data.pairs)
    assert next(p for p in data.pairs if p.task_id == tasks[0].task_id).revision == 2
    for pair in data.pairs:
        if pair.decision in {"left", "right"}:
            actual = pair.example.a if pair.example.choice == "a_better" else pair.example.b
            assert actual.candidate_id == expected[pair.task_id]
    # hash/winner 校验由真实 UI parser执行，适配器不能接纳伪造的展示内容。
    records = [Record.model_validate_json(line) for line in exported.splitlines()]
    unknown = [r.model_dump(mode="json") for r in records]
    next(r for r in unknown if r["record_type"] == "task")["data"]["task"]["candidates"][0].pop("source_id")
    unknown_data = adapt_records([Record.model_validate(r) for r in unknown])
    assert any(c.source_id == "unknown" for p in unknown_data.pairs for c in (p.example.a, p.example.b))
    changed = [r.model_dump(mode="json") for r in records]
    task_record = next(r for r in changed if r["record_type"] == "task")
    task_record["data"]["task"]["candidates"][0]["text"] += "伪造"
    try:
        adapt_records([Record.model_validate(r) for r in changed])
    except ValueError:
        pass
    else:
        raise AssertionError("篡改原文hash未被拒绝")
    try:
        train_and_evaluate(data)
    except ValueError:
        pass
    else:
        raise AssertionError("默认设置不应训练模拟弱标/合成材料")
    model, report = train_and_evaluate(data, allow_synthetic=True, ai_weight=.25, seed=0, epochs=200)
    verify_training_record(model, data, report)
    try:
        verify_training_record(model, data, report | {"training_provenance": {"partition": "test"}})
    except ValueError:
        pass
    else:
        raise AssertionError("训练来源记录不符时不应接受holdout评测")
    write_private(output / "model.json", model.to_json() + "\n")
    assert load_reviewed_ranker(output / "model.json")[1]["status"] == "review_required"
    report["fixture"] = {"notice": fixture["notice"], "http_export": "passed", "winner_mapping": "passed",
                         "latest_revision_withdrawal": "passed", "weak_human_separation": "passed",
                         "human_adapter_protocol": check_adapter_revisions(output, tasks),
                         "input_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest()}
    report["provider"] = check_provider(database_url, output) if database_url else {"status": "not_run"}
    write_private(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relevance-root", type=Path, required=True, help="PR115独立worktree；仅本检查组合imports")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database-url", help="可选显式空的本机research_eval_*库，仅合成provider检查")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    compose(args.relevance_root)
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = run(args.output_dir, args.database_url)
    ui_head = subprocess.run(["git", "-C", str(args.relevance_root), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True).stdout.strip()
    print(json.dumps({"ui_head": ui_head, "model_revision": report["model_revision"],
                      "splits": report["coverage"]["ui_coverage"]["task_counts"],
                      "evaluation": {s: r["model"]["by_source"]["ai"] for s, r in report["evaluation"].items()},
                      "provider": report["provider"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""只用 tmp_path SQLite 和原创合成材料，不连接 Postgres、不调用任何模型。"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from researchlib.relevance.__main__ import main
from researchlib.relevance.demo import demo_records
from researchlib.relevance.grouping import freeze, related
from researchlib.relevance.schema import Candidate, LabelRequest, LabelSource, Manifest, Task
from researchlib.relevance.store import Conflict, Record, Store, read_jsonl, record, validate_records
from researchlib.web.relevance import FORWARDED_HEADERS, create_annotation_app

ROOT = Path(__file__).resolve().parents[2]
HUMAN = LabelSource(kind="human", labeler_id="local-synthetic-annotator")


@pytest.fixture
def store(tmp_path):
    rows = demo_records()
    manifest = validate_records(rows).manifest
    store = Store.initialize(tmp_path / "annotations.sqlite3", manifest)
    store.import_records(rows)
    return store


def client_for(store, **kwargs):
    app = create_annotation_app(store.path, HUMAN.labeler_id, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1:8099", client=("127.0.0.1", 52001))
    client.headers["X-Relevance-CSRF"] = app.state.relevance_csrf
    return client


def request_for(presentation, decision="left", revision=0, request_id=None):
    return LabelRequest(
        presentation_id=presentation.presentation_id,
        decision=decision,
        expected_revision=revision,
        request_id=request_id or str(uuid.uuid4()),
    )


def variant(task, suffix, **changes):
    candidates = []
    for index, c in enumerate(task.candidates):
        data = c.model_dump(exclude={"content_hash", "candidate_id"}) | {
            "article_id": f"new-{suffix}-{index}",
            "document_family_id": f"family-{suffix}-{index}",
            "text": f"全新独立的虚构材料 {suffix} 第{index}段，用来检查分组关系和稳定导入边界。",
        }
        candidates.append(Candidate.model_validate(data))
    data = (
        task.model_dump(exclude={"query_id", "task_id"})
        | {
            "query": f"独立测试查询 {suffix}",
            "entity_ids": [f"entity-{suffix}"],
            "topic_ids": [f"topic-{suffix}"],
            "candidates": candidates,
        }
        | changes
    )
    return Task.model_validate(data)


def test_fixture_reproducible_and_only_synthetic():
    actual = read_jsonl(ROOT / "tools/relevance/demo.jsonl")
    assert actual == demo_records()
    dataset = validate_records(actual)
    assert len(dataset.tasks) == 3 and not dataset.events
    assert all(item.task.material == "synthetic" for item in dataset.tasks)


def test_schema_checks_content_version_hash_and_pair_identity(store):
    task = store.snapshot().tasks[0].task
    reversed_task = Task.model_validate(task.model_dump() | {"candidates": tuple(reversed(task.candidates))})
    assert task == reversed_task
    candidate = task.candidates[0]
    for update in ({"text": "篡改原文"}, {"content_hash": "bad"}, {"candidate_id": "bad"}):
        with pytest.raises(ValidationError):
            Candidate.model_validate(candidate.model_dump() | update)
    changed = Candidate.model_validate(candidate.model_dump(exclude={"candidate_id"}) | {"content_version": "v2"})
    assert changed.candidate_id != candidate.candidate_id and changed.content_hash == candidate.content_hash
    with pytest.raises(ValidationError):
        Task.model_validate(task.model_dump() | {"candidates": (candidate, candidate)})


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "//attacker.test/x",
        "/articles/1",
        "https://user:password@example.invalid/",
    ],
)
def test_reject_unsafe_source_urls(store, url):
    with pytest.raises(ValidationError):
        Candidate.model_validate(store.snapshot().tasks[0].task.candidates[0].model_dump() | {"source_url": url})


def test_display_context_sources_and_persisted_random_mapping(store, monkeypatch):
    client = client_for(store)
    page = client.get("/annotate")
    assert page.status_code == 200 and "哪段原文" in page.text and "CSRF" not in page.text
    assert "relevance-csrf" in page.text and "不是" not in page.headers["content-security-policy"]
    monkeypatch.setattr("researchlib.relevance.store.secrets.randbits", lambda bits: 1)
    first = client.get("/api/relevance/state").json()["current"]
    task = next(f.task for f in store.snapshot().tasks if f.task.task_id == first["task_id"])
    assert first["left"]["candidate_id"] == task.candidates[1].candidate_id
    monkeypatch.setattr("researchlib.relevance.store.secrets.randbits", lambda bits: 0)
    again = client_for(Store(store.path)).get("/api/relevance/state").json()["current"]
    assert first == again
    for side in ("left", "right"):
        c = first[side]
        assert c["text"] and c["context_before"] and c["context_after"] and c["article_title"]
        source = client.get(c["source_url"])
        assert source.status_code == 200 and c["text"] in source.text and "合成" in source.text
    assert page.headers["cache-control"] == "private, no-store"


def test_both_random_outcomes_record_actual_ids(store, monkeypatch):
    task = store.snapshot().tasks[0].task
    for bit, actor in ((0, "zero"), (1, "one")):
        monkeypatch.setattr("researchlib.relevance.store.secrets.randbits", lambda bits, bit=bit: bit)
        p = store.present(task.task_id, actor)
        assert p.left_id == task.candidates[bit].candidate_id
        source = LabelSource(kind="human", labeler_id=actor)
        assert store.label(request_for(p), source).winner_id == p.left_id


@pytest.mark.parametrize("decision", ["left", "right", "tie", "neither", "skip", "uncertain"])
def test_all_decisions_idempotency_restart_and_export_replay(store, tmp_path, decision):
    client = client_for(store)
    current = client.get("/api/relevance/state").json()["current"]
    body = {
        "presentation_id": current["presentation_id"],
        "decision": decision,
        "expected_revision": 0,
        "request_id": "same-request",
    }
    first = client.post("/api/relevance/labels", json=body)
    assert first.status_code == 200
    event = first.json()
    expected = (
        current["left"]["candidate_id"]
        if decision == "left"
        else (current["right"]["candidate_id"] if decision == "right" else None)
    )
    assert event["winner_id"] == expected
    assert event["source"] == HUMAN.model_dump() and event["split"] == current["split"]
    assert client.post("/api/relevance/labels", json=body).json() == event
    body["decision"] = "right" if decision != "right" else "left"
    assert client.post("/api/relevance/labels", json=body).status_code == 409
    assert len(store.snapshot().events) == 1
    recovered = client_for(Store(store.path)).get("/api/relevance/state").json()
    assert recovered["completed"] == 1 and recovered["previous"]["decision"] == decision
    exported = client.get("/api/relevance/export").text
    path = tmp_path / "private.jsonl"
    path.write_text(exported, encoding="utf-8")
    records = read_jsonl(path)
    replay = Store.initialize(tmp_path / "replay.sqlite3", store.snapshot().manifest)
    replay.import_records(records)
    replay.import_records(records)
    assert replay.export() == store.export()
    assert len(replay.snapshot().events) == 1
    assert client_for(replay).get("/api/relevance/state").json() == recovered


def test_correction_withdraw_and_stale_tab_conflict(store):
    client = client_for(store)
    current = client.get("/api/relevance/state").json()["current"]
    p = next(p for p in store.snapshot().presentations if p.presentation_id == current["presentation_id"])
    first = store.label(request_for(p, "left"), HUMAN)
    corrected = store.label(request_for(p, "right", 1), HUMAN)
    assert corrected.winner_id == p.right_id and first.winner_id == p.left_id
    with pytest.raises(Conflict, match="版本"):
        store.label(request_for(p, "neither", 1), HUMAN)
    withdrawn = store.label(request_for(p, "withdraw", 2), HUMAN)
    assert withdrawn.winner_id is None
    state = client.get("/api/relevance/state").json()
    assert state["completed"] == 0 and state["current"]["presentation_id"] == p.presentation_id
    assert state["current"]["revision"] == 3
    store.label(request_for(p, "tie", 3), HUMAN)
    assert [e.revision for e in sorted(store.snapshot().events, key=lambda e: e.revision)] == [1, 2, 3, 4]


def test_simultaneous_double_click_creates_one_event(store):
    task = store.snapshot().tasks[0].task
    p = store.present(task.task_id, HUMAN.labeler_id)

    def save(_):
        try:
            return store.label(request_for(p), HUMAN)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert sum(value is not None for value in results) == 1
    assert len(store.snapshot().events) == 1


def test_same_pair_reimport_or_reverse_has_no_duplicate(store):
    tasks = store.snapshot().tasks
    task = tasks[0].task
    store.add_tasks([task, Task.model_validate(task.model_dump() | {"candidates": list(reversed(task.candidates))})])
    store.import_records(demo_records())
    assert len(store.snapshot().tasks) == len(tasks)
    with pytest.raises(ValueError, match="元数据"):
        store.add_tasks([Task.model_validate(task.model_dump() | {"topic_ids": ["different-topic"]})])


@pytest.mark.parametrize("link", ["entity", "topic", "family", "article", "normalized", "near_duplicate"])
def test_groups_follow_entities_topics_families_and_near_duplicates(store, link):
    initial = store.snapshot().tasks[0]
    original = initial.task
    updates = {}
    if link == "entity":
        updates["entity_ids"] = original.entity_ids
    elif link == "topic":
        updates["topic_ids"] = original.topic_ids
    changed = variant(original, link, **updates)
    if link in {"family", "article", "normalized", "near_duplicate"}:
        candidates = list(changed.candidates)
        old = original.candidates[0]
        data = candidates[0].model_dump(exclude={"content_hash", "candidate_id"})
        if link == "family":
            data["document_family_id"] = old.document_family_id
        elif link == "article":
            data["article_id"] = old.article_id
        elif link == "normalized":
            data["text"] = old.text.replace("，", " , ").replace("。", ". \n")
        else:
            data["text"] = old.text + "新增。"
        candidates[0] = Candidate.model_validate(data)
        changed = Task.model_validate(changed.model_dump(exclude={"query_id", "task_id"}) | {"candidates": candidates})
    assert related(original, changed)
    result = store.add_tasks([changed])
    frozen = next(item for item in result if item.task.task_id == changed.task_id)
    assert (frozen.group_id, frozen.split) == (initial.group_id, initial.split)


def test_new_bridge_cannot_move_frozen_groups_and_import_is_atomic(store):
    dataset = store.snapshot()
    a, b = dataset.tasks[:2]
    assert a.group_id != b.group_id
    bridge = variant(a.task, "bridge", entity_ids=a.task.entity_ids + b.task.entity_ids)
    before = store.export("all")
    with pytest.raises(ValueError, match="多个已冻结"):
        store.add_tasks([bridge])
    assert store.export("all") == before
    hacked = b.model_dump() | {"group_id": a.group_id, "split": "test" if a.split != "test" else "train"}
    rows = [
        record("manifest", dataset.manifest),
        Record(record_type="task", data=a.model_dump()),
        Record(record_type="task", data=hacked),
    ]
    with pytest.raises(ValueError):
        store.import_records(rows)
    assert store.export("all") == before


def test_deterministic_component_freezing_is_order_independent(store):
    dataset = store.snapshot()
    tasks = [f.task for f in dataset.tasks]
    assert freeze([], tasks, dataset.manifest) == freeze([], list(reversed(tasks)), dataset.manifest)
    assert freeze(dataset.tasks, tasks, dataset.manifest) == dataset.tasks


def test_ai_weak_never_becomes_human_truth_or_completes_human_queue(store):
    task = store.snapshot().tasks[0].task
    p = store.present(task.task_id, HUMAN.labeler_id)
    weak = LabelSource(
        kind="ai_weak",
        labeler_id=HUMAN.labeler_id,
        model_name="synthetic-stub",
        model_version="0",
        prompt_version="fixture-v1",
    )
    store.label(request_for(p), weak)
    assert not validate_records([Record.model_validate_json(line) for line in store.export().splitlines()]).events
    assert client_for(store).get("/api/relevance/state").json()["completed"] == 0
    store.label(request_for(p, "neither"), HUMAN)
    assert len(store.snapshot().events) == 2
    for source in ("human", "ai_weak"):
        dataset = validate_records([Record.model_validate_json(line) for line in store.export(source).splitlines()])
        assert len(dataset.events) == 1 and dataset.events[0].source.kind == source


@pytest.mark.parametrize("header", sorted(FORWARDED_HEADERS))
def test_forwarded_public_request_is_denied_before_any_database_access(tmp_path, header):
    absent = Store(tmp_path / "must-not-be-created.sqlite3")
    client = client_for(absent)
    assert client.get("/annotate", headers={header: "spoofed"}).status_code == 403
    assert client.get("/api/relevance/state", headers={header: "spoofed"}).status_code == 403
    assert not absent.path.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://attacker.invalid"},
        {"client": ("203.0.113.10", 6000)},
        {"headers": {"Origin": "https://evil.invalid"}},
        {"headers": {"Sec-Fetch-Site": "cross-site"}},
    ],
)
def test_local_peer_host_and_origin_gate(tmp_path, changes):
    app = create_annotation_app(tmp_path / "absent.sqlite3", "operator")
    options = {"base_url": "http://127.0.0.1", "client": ("127.0.0.1", 6100)} | changes
    client = TestClient(app, **options)
    assert client.get("/annotate").status_code == 403
    assert not (tmp_path / "absent.sqlite3").exists()


def test_post_requires_csrf_and_cannot_choose_identity_or_source(store):
    client = client_for(store)
    current = client.get("/api/relevance/state").json()["current"]
    body = {
        "presentation_id": current["presentation_id"],
        "decision": "left",
        "expected_revision": 0,
        "request_id": "x",
    }
    assert client.post("/api/relevance/labels", json=body, headers={"X-Relevance-CSRF": ""}).status_code == 403
    assert client.post("/api/relevance/labels", json=body | {"annotator_id": "another-user"}).status_code == 422
    assert client.post("/api/relevance/labels", json=body | {"source": {"kind": "human"}}).status_code == 422
    assert not store.snapshot().events
    with pytest.raises(LookupError):
        store.label(LabelRequest.model_validate(body), LabelSource(kind="human", labeler_id="someone-else"))


def test_demo_does_not_answer_arbitrary_query_and_explicit_provider_receives_it(store):
    client = client_for(store)
    assert client.post("/api/relevance/generate", json={"query": "另一个查询"}).status_code == 503
    calls = []
    original = store.snapshot().tasks[0].task

    def provider(*, query, scope):
        calls.append((query, scope))
        return variant(original, "provider", query=query, scope=scope, material="private_corpus")

    configured = client_for(store, candidate_provider=provider)
    body = {"query": " 新的问题  ", "scope": {"source": "explicit-test"}}
    result = configured.post("/api/relevance/generate", json=body)
    assert result.status_code == 200 and calls == [("新的问题", {"source": "explicit-test"})]
    assert configured.post("/api/relevance/generate", json=body).json() == result.json()
    assert len(store.snapshot().tasks) == 4
    bad = client_for(store, candidate_provider=lambda **kwargs: original)
    assert bad.post("/api/relevance/generate", json={"query": "错配查询"}).status_code == 422


def test_invalid_hash_mapping_revision_and_split_cannot_be_imported(store):
    task = store.snapshot().tasks[0].task
    p = store.present(task.task_id, HUMAN.labeler_id)
    store.label(request_for(p), HUMAN)
    rows = [Record.model_validate_json(line) for line in store.export().splitlines()]
    index = next(i for i, row in enumerate(rows) if row.record_type == "label_event")
    for change in ({"winner_id": p.right_id}, {"revision": 2}, {"split": "not-a-split"}, {"created_at": "2026-01-01"}):
        with pytest.raises(ValueError):
            changed = rows[:]
            changed[index] = Record.model_validate(rows[index].model_dump() | {"data": rows[index].data | change})
            validate_records(changed)
    assert len(store.snapshot().events) == 1


def test_cli_round_trip_file_permissions_and_non_destructive_init(tmp_path, capsys):
    db = tmp_path / "private.sqlite3"
    main(["demo", "--db", str(db)])
    assert db.stat().st_mode & 0o777 == 0o600
    output = tmp_path / "private.jsonl"
    main(["export", "--db", str(db), "--output", str(output)])
    assert output.stat().st_mode & 0o777 == 0o600
    main(["check", str(output)])
    assert "校验通过" in capsys.readouterr().out
    restored = tmp_path / "restored.sqlite3"
    main(["import", "--db", str(restored), "--input", str(output)])
    assert Store(restored).export() == Store(db).export()
    before = db.read_bytes()
    with pytest.raises(SystemExit):
        main(["init", "--db", str(db), "--dataset-id", "other", "--seed", "seed"])
    assert db.read_bytes() == before
    with pytest.raises(SystemExit):
        main(["export", "--db", str(db), "--output", str(output)])


def test_standalone_import_and_client_block_all_business_and_model_modules(tmp_path):
    script = """
import importlib.abc, sys
from pathlib import Path
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = {"researchlib.db", "researchlib.ask", "researchlib.llm",
                   "researchlib.embeddings", "researchlib.retrieval", "researchlib.web.app"}
        if fullname in blocked:
            raise AssertionError("禁止导入业务/模型模块: " + fullname)
sys.meta_path.insert(0, Block())
from researchlib.relevance.__main__ import main
from researchlib.web.relevance import create_annotation_app
from fastapi.testclient import TestClient
path=Path(sys.argv[1])
main(["demo", "--db", str(path)])
app=create_annotation_app(path,"isolated")
client=TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",1))
assert client.get("/annotate").status_code==200
state=client.get("/api/relevance/state").json()
assert len(state["current"]["left"]["text"])>0
response=client.post("/api/relevance/generate",json={"query":"x"},
                     headers={"X-Relevance-CSRF":app.state.relevance_csrf})
assert response.status_code==503
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "safe.sqlite3")], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_collapsed_groups_warn_in_report_ui_check_and_export(tmp_path, capsys):
    dataset = validate_records(demo_records())
    manifest = Manifest(dataset_id="collapsed-synthetic-test", seed="frozen-seed")
    tasks = [Task.model_validate(f.task.model_dump() | {"entity_ids": ["shared-entity"]}) for f in dataset.tasks]
    store = Store.initialize(tmp_path / "collapsed.sqlite3", manifest)
    frozen = store.add_tasks(tasks)
    assert len({f.group_id for f in frozen}) == 1
    assert len({f.split for f in frozen}) == 1
    report = store.snapshot().report()
    assert "single_connected_group" in {w["code"] for w in report["warnings"]}
    assert report["largest_group_share"] == 1
    client = client_for(store)
    assert client.get("/api/relevance/state").json()["coverage"] == report
    assert client.get("/api/relevance/report").json() == report
    exported = client.get("/api/relevance/export")
    assert "single_connected_group" in exported.headers["x-relevance-split-warnings"]
    output = tmp_path / "collapse-private.jsonl"
    main(["export", "--db", str(store.path), "--output", str(output)])
    assert "全部任务坍缩" in capsys.readouterr().err
    sidecar = output.with_name(output.name + ".report.json")
    assert json.loads(sidecar.read_text()) == report
    assert sidecar.stat().st_mode & 0o777 == 0o600
    main(["check", str(output)])
    assert "无法构成独立 holdout" in capsys.readouterr().err
    assert len({f.split for f in store.snapshot().tasks}) == 1


def test_source_id_optional_unknown_and_import_export_preserve_explicit_identity(store, tmp_path):
    candidate = store.snapshot().tasks[0].task.candidates[0]
    original = candidate.model_dump()
    omitted = original.copy()
    omitted.pop("source_id")
    assert Candidate.model_validate(omitted).source_id == "unknown"
    assert Candidate.model_validate(original | {"source_id": None}).source_id == "unknown"
    assert (
        Candidate.model_validate(original | {"source_id": "explicit-source-v1"}).candidate_id == candidate.candidate_id
    )
    output = tmp_path / "source-identity.jsonl"
    output.write_text(store.export(), encoding="utf-8")
    replay = Store.initialize(tmp_path / "source-replay.sqlite3", store.snapshot().manifest)
    replay.import_records(read_jsonl(output))
    assert {c.source_id for f in replay.snapshot().tasks for c in f.task.candidates} == {"synthetic-fixture"}
    assert replay.export() == store.export()

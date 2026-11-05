"""合成资料上的对照隔离、回放、人工评价、调用去重和访问边界。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from researchlib.experiments.capture import validate_dsn
from researchlib.experiments.demo import demo_snapshot
from researchlib.experiments.engine import compare
from researchlib.experiments.schema import CompareRequest, QuerySpec, RatingRequest, Snapshot, Tuning
from researchlib.experiments.store import Conflict, Store
from researchlib.retrieval import K1, ScoreParameters
from researchlib.web.experiments import create_experiment_app


@pytest.fixture
def snapshot():
    # 真实 JSON 往返也检查 dataclass 中 tuple、int-key 字典和时间回放。
    return Snapshot.model_validate_json(demo_snapshot().model_dump_json())


@pytest.fixture
def store(snapshot, tmp_path):
    return Store.initialize(tmp_path / "experiments.sqlite3", snapshot.fingerprint)


def client_for(snapshot, store, **kwargs):
    app = create_experiment_app(snapshot, store.path, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1:8100", client=("127.0.0.1", 123))
    client.headers["x-experiment-csrf"] = app.state.experiment_csrf
    return client


def start(client, **kwargs):
    response = client.post("/api/experiments/compare", json={"case_ids": ["demo-1", "demo-2"], **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def rating(case="demo-1", decision="candidate", revision=0, request="request-0001", target="search"):
    return RatingRequest(case_id=case, target=target, decision=decision,
                         expected_revision=revision, request_id=request)


@pytest.mark.parametrize("field,value", [
    ("k1", 0), ("b", 1.1), ("title_tf", -1), ("title_bonus", 3), ("phrase_bonus", 3),
    ("coverage_power", 0), ("min_score_ratio", 0.6), ("passages", 81), ("chars", 32001),
    ("per_article", 0), ("k1", True), ("b", "0.4"), ("passages", 3.1), ("k1", float("nan")),
    ("k1", float("inf")), ("unexpected", 1), ("prompt_note", "甲" * 2001),
])
def test_tuning_rejects_unsafe_values(field, value):
    with pytest.raises(ValidationError):
        Tuning.model_validate({field: value})


def test_defaults_same_but_candidate_isolated_and_replayable(snapshot):
    request = CompareRequest(case_ids=["demo-1"])
    baseline = compare(snapshot, request)
    case = baseline["cases"][0]
    assert case["baseline"]["search"] == case["candidate"]["search"]
    assert case["baseline"]["messages"] == case["candidate"]["messages"]
    changed = request.model_copy(update={"candidate": Tuning(title_tf=0, title_bonus=0, passages=1)})
    before = snapshot.model_dump_json()
    with ThreadPoolExecutor(max_workers=5) as pool:
        runs = list(pool.map(lambda _: compare(snapshot, changed), range(8)))
    assert all(r["id"] == runs[0]["id"] for r in runs)
    assert runs[0]["id"] != baseline["id"]
    assert all(r["cases"][0]["baseline"]["search"] == case["baseline"]["search"] for r in runs)
    assert all(len(r["cases"][0]["candidate"]["passages"]) == 1 for r in runs)
    assert snapshot.model_dump_json() == before and K1 == 1.2
    assert ScoreParameters().k1 == 1.2
    replay = compare(Snapshot.model_validate_json(before), changed)
    assert replay["id"] == runs[0]["id"]
    assert replay["cases"][0]["candidate"]["messages"] == runs[0]["cases"][0]["candidate"]["messages"]


def test_prompt_only_changes_answer_input(snapshot):
    request = CompareRequest(case_ids=["demo-1"], candidate=Tuning(prompt_note="先比较分歧"))
    case = compare(snapshot, request)["cases"][0]
    assert case["baseline"]["search"] == case["candidate"]["search"]
    assert case["baseline"]["passages"] == case["candidate"]["passages"]
    assert "先比较分歧" in case["candidate"]["messages"][0]["content"]
    assert "先比较分歧" not in case["baseline"]["messages"][0]["content"]


def test_unknown_and_duplicate_questions_rejected(snapshot):
    with pytest.raises(ValueError):
        compare(snapshot, CompareRequest(case_ids=["unknown"]))
    with pytest.raises(ValidationError):
        CompareRequest(case_ids=["demo-1", "demo-1"])


def test_empty_candidate_not_given_a_fake_answer(snapshot):
    frozen = snapshot.cases[0].model_copy(update={"rows": [], "qa_rows": []})
    value = compare(snapshot.model_copy(update={"cases": [frozen]}), CompareRequest(case_ids=[frozen.spec.id]))
    side = value["cases"][0]["baseline"]
    assert side["search"] == [] and side["passages"] == []
    assert not side["generation_eligible"]
    assert value["cases"][0]["search_overlap_at_10"] is None


def test_oversize_first_passage_never_truncated_or_generated(snapshot):
    case = snapshot.cases[0]
    row = case.qa_rows[0].model_copy(update={"body": "会员制零售" * 2000})
    case = case.model_copy(update={"rows": [row], "qa_rows": [row]})
    side = compare(snapshot.model_copy(update={"cases": [case]}), CompareRequest(
        case_ids=[case.spec.id], candidate=Tuning(chars=1000), baseline=Tuning(chars=1000),
    ))["cases"][0]["baseline"]
    assert not side["generation_eligible"] and not side["passages"]


def test_context_order_and_content_preserved(snapshot):
    case = snapshot.cases[0]
    row = case.qa_rows[0].model_copy(update={"ord": 1})
    before = row.model_copy(update={"ord": 0, "body": "上文 −10% 与 +10% 不能混淆"})
    after = row.model_copy(update={"ord": 2, "body": "下文 10.5% 与 105% 不能混淆"})
    case = case.model_copy(update={"qa_rows": [row], "neighbours": [before, after]})
    side = compare(snapshot.model_copy(update={"cases": [case]}), CompareRequest(case_ids=[case.spec.id]))
    passage = side["cases"][0]["baseline"]["passages"][0]
    assert passage["before"] == before.body and passage["after"] == after.body
    text = side["cases"][0]["baseline"]["messages"][1]["content"]
    assert text.index(before.body) < text.index(row.body) < text.index(after.body)


@pytest.mark.parametrize("scope", [
    {"source_ids": []}, {"source_ids": ["demo-a"], "article_id": "nonsense"},
    {"source_ids": ["demo-a"], "article_id": -1},
    {"source_ids": ["demo-a"], "company_id": True},
    {"source_ids": ["demo-a"], "date_from": "2026-12-01", "date_to": "2026-11-01"},
    {"source_ids": ["demo-a"], "anything": 3},
])
def test_scope_never_silently_widened(scope):
    with pytest.raises(ValidationError):
        QuerySpec(id="query", query="现金流", scope=scope)


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@127.0.0.1/research", "postgresql://u:p@remote.example/research_eval",
    "dbname=research_eval", "host=127.0.0.1 dbname=research_eval hostaddr=192.0.2.1",
    "host=localhost dbname=research_test service=production", "host=localhost dbname=research_test options=anything",
])
def test_capture_rejects_production_or_indirect_targets(dsn):
    with pytest.raises(ValueError):
        validate_dsn(dsn)


def test_capture_accepts_only_explicit_local_copy():
    assert validate_dsn("host=127.0.0.1 dbname=research_experiment")["dbname"] == "research_experiment"


def test_run_reused_and_store_bound_to_snapshot(snapshot, store):
    client = client_for(snapshot, store)
    first, second = start(client), start(client)
    assert first == second and len(store.history()) == 1
    assert first["model_calls"] == 0 and first["cost"] is None
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        Store.initialize(store.path, snapshot.fingerprint)
    with pytest.raises(ValueError):
        Store(store.path, "wrong-snapshot")
    assert Store(store.path, snapshot.fingerprint).get(first["id"]) == first
    exported = client.get(f"/api/experiments/runs/{first['id']}/export")
    assert exported.status_code == 200 and exported.json()["run"] == first
    assert exported.headers["cache-control"] == "private, no-store"
    downloaded = Snapshot.model_validate(client.get("/api/experiments/snapshot").json())
    assert downloaded.fingerprint == snapshot.fingerprint


@pytest.mark.parametrize("decision", ["baseline", "candidate", "tie", "neither", "skip", "uncertain", "withdraw"])
def test_ratings_idempotent_correctable_and_summary_separate(snapshot, store, decision):
    client = client_for(snapshot, store)
    run = start(client)
    event = store.label(run["id"], rating(decision=decision))
    assert store.label(run["id"], rating(decision=decision)) == event
    summary = store.summary(run["id"])["search"]
    assert summary["counts"][decision] == 1
    assert summary["directional_n"] == int(decision in {"baseline", "candidate"})
    if decision not in {"baseline", "candidate"}:
        assert summary["candidate_preference"] is None
    with pytest.raises(Conflict):
        store.label(run["id"], rating(decision="tie", request="request-0002"))
    second = store.label(run["id"], rating(decision="withdraw", revision=1, request="request-0003"))
    assert second["revision"] == 2 and len(store.ratings(run["id"])) == 2
    assert store.summary(run["id"])["search"]["directional_n"] == 0
    with pytest.raises(Conflict):
        store.label(run["id"], rating(case="demo-2", decision="neither"))


def test_concurrent_ratings_do_not_overwrite(snapshot, store):
    run = start(client_for(snapshot, store))
    def save(index):
        try:
            return store.label(run["id"], rating(request=f"request-{index:04d}"))
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(save, range(2)))
    assert sum(r is not None for r in result) == 1
    assert len(store.ratings(run["id"])) == 1


@pytest.mark.parametrize("headers", [
    {"origin": "https://remote.example"}, {"host": "remote.example"}, {"x-forwarded-for": "127.0.0.1"},
    {"cf-connecting-ip": "127.0.0.1"}, {"sec-fetch-site": "cross-site"}, {"forwarded": "for=127.0.0.1"},
])
def test_all_reads_and_writes_reject_nonlocal_paths(snapshot, store, headers):
    client = client_for(snapshot, store)
    assert client.get("/api/experiments/state", headers=headers).status_code == 403
    assert client.post("/api/experiments/compare", headers=headers, json={"case_ids": ["demo-1"]}).status_code == 403


def test_csrf_remote_peer_and_oversize_body(snapshot, store):
    client = client_for(snapshot, store)
    client.headers.pop("x-experiment-csrf")
    assert client.post("/api/experiments/compare", json={"case_ids": ["demo-1"]}).status_code == 403
    app = create_experiment_app(snapshot, store.path)
    remote = TestClient(app, base_url="http://localhost", client=("192.0.2.1", 123))
    assert remote.get("/experiments").status_code == 403
    client.headers["x-experiment-csrf"] = app.state.experiment_csrf
    client = client_for(snapshot, store)
    assert client.post("/api/experiments/compare", content=b"x" * 65537).status_code == 413


def test_source_policy_rechecked_on_every_read_and_write(snapshot, store, tmp_path):
    import yaml
    snapshot = snapshot.model_copy(update={"material": "private_corpus"})
    store = Store.initialize(tmp_path / "private.sqlite3", snapshot.fingerprint)
    path = tmp_path / "sources.yaml"
    def write(enabled):
        path.write_text(yaml.safe_dump({"sources": [
            {"id": "demo-a", "name": "虚构研究札记", "author": "王小明", "kind": "markdown", "enabled": enabled},
            {"id": "demo-b", "name": "虚构观察周报", "author": "李四", "kind": "markdown", "enabled": True},
        ]}, allow_unicode=True))
    write(True)
    with pytest.raises(ValueError):
        create_experiment_app(snapshot, store.path)
    client = client_for(snapshot, store, sources_path=path)
    run = start(client)
    write(False)
    assert client.get(f"/api/experiments/runs/{run['id']}/export").status_code == 403
    assert client.post("/api/experiments/compare", json=run["request"]).status_code == 403
    with pytest.raises(ValueError):
        create_experiment_app(snapshot, store.path, sources_path=path)


def test_source_endpoint_bounded_and_preserves_original(snapshot, store):
    client = client_for(snapshot, store)
    row = client.get("/api/experiments/source/demo-1/1/0")
    assert row.status_code == 200 and row.json()["body"] == snapshot.cases[0].rows[0].body
    assert row.json()["snapshot_id"] == snapshot.fingerprint
    assert client.get("/api/experiments/source/demo-1/999/0").status_code == 404
    assert client.get("/api/experiments/source/unknown/1/0").status_code == 404


def test_no_generator_default_and_answers_cannot_be_rated_early(snapshot, store):
    client = client_for(snapshot, store)
    run = start(client)
    assert client.get("/api/experiments/state").json()["generator"] is None
    response = client.post(f"/api/experiments/runs/{run['id']}/generate", json={
        "case_id": "demo-1", "acknowledge_cost": True, "request_id": "generate-0001",
    })
    assert response.status_code == 503 and store.generation(run["id"], "demo-1") is None
    with pytest.raises(ValueError, match="两边答案"):
        store.label(run["id"], rating(target="answer"))


def answer():
    return {"text": "虚构离线回答 [1]", "model": "synthetic-stub", "model_version": "test-v1"}


def test_generation_explicit_ack_and_no_retry(snapshot, store):
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return answer()
    client = client_for(snapshot, store, generator=generate, generator_label="synthetic-stub:v1")
    run = start(client)
    route = f"/api/experiments/runs/{run['id']}/generate"
    body = {"case_id": "demo-1", "request_id": "generate-0001"}
    assert client.post(route, json=body).status_code == 422
    assert calls == []
    body["acknowledge_cost"] = True
    first = client.post(route, json=body).json()
    assert first["status"] == "done" and len(calls) == 2
    assert all(c["max_output_tokens"] == 1500 for c in calls)
    assert first["sides"]["baseline"]["cost"] is None
    assert client.post(route, json=body).json() == first
    body["request_id"] = "generate-new-id"
    assert client.post(route, json=body).json() == first and len(calls) == 2
    store.label(run["id"], rating(target="answer"))
    assert store.summary(run["id"])["answer"]["directional_n"] == 1


def test_generation_partial_failure_keeps_success_and_never_retries(snapshot, store):
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("private-secret-must-not-be-returned")
        return answer()
    client = client_for(snapshot, store, generator=generate, generator_label="synthetic-stub:v1")
    run = start(client)
    route = f"/api/experiments/runs/{run['id']}/generate"
    body = {"case_id": "demo-1", "acknowledge_cost": True, "request_id": "generate-0001"}
    response = client.post(route, json=body)
    assert response.json()["status"] == "failed"
    assert "baseline" in response.json()["sides"] and "candidate" not in response.json()["sides"]
    assert "private-secret" not in response.text
    assert client.post(route, json=body).json() == response.json() and len(calls) == 2
    with pytest.raises(ValueError):
        store.label(run["id"], rating(target="answer"))


def test_concurrent_generation_only_one_pair_and_interruption_unknown(snapshot, store):
    begun, finish = Event(), Event()
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        begun.set()
        assert finish.wait(5)
        return answer()
    client = client_for(snapshot, store, generator=generate, generator_label="synthetic-stub:v1")
    run = start(client)
    route = f"/api/experiments/runs/{run['id']}/generate"
    body = {"case_id": "demo-1", "acknowledge_cost": True, "request_id": "generate-0001"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, route, json=body)
        assert begun.wait(5)
        second = client.post(route, json=body).json()
        assert second["status"] == "running"
        finish.set()
        assert pending.result().json()["status"] == "done"
    assert len(calls) == 2
    # 模拟进程已退出但占位仍存在：新的页面/进程不会把未知结果变成再次调用。
    store.claim_generation(run["id"], "demo-2", "crashed-request", "synthetic-stub:v1")
    new_client = client_for(snapshot, store, generator=generate, generator_label="synthetic-stub:v1")
    body.update(case_id="demo-2", request_id="another-request")
    assert new_client.post(route, json=body).json()["status"] == "running"
    assert len(calls) == 2


def test_static_ui_uses_text_nodes_and_main_has_no_experiment_router():
    root = Path(__file__).resolve().parents[2]
    js = (root / "src/researchlib/web/static/experiments.js").read_text()
    assert "innerHTML" not in js and "textContent" in js
    assert "create_experiment_app" not in (root / "src/researchlib/web/app.py").read_text()


def test_snapshot_rejects_cross_scope_and_disabled_sources(snapshot):
    payload = json.loads(snapshot.model_dump_json())
    payload["cases"][0]["rows"][0]["source_id"] = "outside-scope"
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)
    payload = json.loads(snapshot.model_dump_json())
    payload["sources"][0]["enabled"] = False
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


def test_frozen_clock_makes_recency_replay_stable(snapshot):
    case = snapshot.cases[0]
    case.query.recent = True
    original = compare(snapshot, CompareRequest(case_ids=[case.spec.id]))
    future = replace(case.context, as_of=case.context.as_of.replace(year=2030))
    changed = case.model_copy(update={"context": future})
    other = compare(snapshot.model_copy(update={"cases": [changed]}), CompareRequest(case_ids=[case.spec.id]))
    assert original["cases"][0]["baseline"]["search"][0]["score"] > other["cases"][0]["baseline"]["search"][0]["score"]

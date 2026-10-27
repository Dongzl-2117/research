"""站内摘要/观点/论点接口：触发、单例、状态、停止、跨站拦截。不连库、不调真 LLM。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from researchlib.config import Settings
from researchlib.jobs import digest as digest_job
from researchlib.jobs import theses as theses_job
from researchlib.jobs import views as views_job
from researchlib.llm import LLMError
from researchlib.web.api import api

ROOT = Path(__file__).resolve().parents[2]


class FakeConn:
    def __init__(self):
        self.rolled_back = 0
        self.closed = False

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True

    def execute(self, *args, **kwargs):
        class _R:
            def fetchone(self_inner):
                return None

            def fetchall(self_inner):
                return []

        return _R()


@pytest.fixture
def isolated_jobs(monkeypatch):
    """每个测试一份干净 worker 状态。"""
    jobs = [digest_job, views_job, theses_job]
    for mod in jobs:
        monkeypatch.setattr(mod._worker, "_thread", None)
        monkeypatch.setattr(mod._worker, "_stop", threading.Event())
        monkeypatch.setattr(mod._worker, "_state", mod._worker._blank())
        monkeypatch.setattr(mod, "shared_credentials_problem", lambda settings: "")
        monkeypatch.setattr(mod, "credentials_problem", lambda settings: "")
        monkeypatch.setattr(mod, "connect", lambda settings: FakeConn())
    yield jobs
    for mod in jobs:
        mod._worker.stop_event.set()
        if mod._worker._thread is not None:
            mod._worker._thread.join(timeout=3)


def _client(tmp_path) -> TestClient:
    settings = Settings.from_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
            "AIHOT_MAP_FILE": str(ROOT / "config" / "aihot_import.yaml"),
            "DISPLAY_TIMEZONE": "Asia/Shanghai",
        }
    )
    app = FastAPI()
    app.state.settings = settings
    app.include_router(api)
    return TestClient(app)


def _wait_stopped(mod, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = mod.status()
        if not snapshot["running"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"{mod._worker.name} 没停下：{mod.status()}")


def test_digest_post_get_stop_singleton(tmp_path, isolated_jobs, monkeypatch):
    monkeypatch.setattr(digest_job, "pending_articles", lambda conn, source_ids, limit: [])
    client = _client(tmp_path)

    first = client.post("/api/v1/digest", json={"limit": 10, "loop": True, "sleep": 5})
    assert first.status_code == 200
    body = first.json()
    assert body["started"] is True
    assert body["running"] is True
    assert body["kind"] == "digest"
    thread_ident = digest_job._worker._thread.ident

    second = client.post("/api/v1/digest", json={"loop": True})
    assert second.status_code == 200
    assert second.json()["started"] is False
    assert digest_job._worker._thread.ident == thread_ident

    status = client.get("/api/v1/digest")
    assert status.status_code == 200
    assert status.json()["running"] is True
    assert "sk-" not in status.text

    stopped = client.post("/api/v1/digest/stop")
    assert stopped.status_code == 200
    final = _wait_stopped(digest_job)
    assert final["running"] is False
    assert final["finished_at"]


def test_digest_counts_completed(tmp_path, isolated_jobs, monkeypatch):
    rows = [
        {"id": 11, "title": "甲", "source_id": "fangwei-mp"},
        {"id": 12, "title": "乙", "source_id": "fangwei-mp"},
    ]
    monkeypatch.setattr(digest_job, "pending_articles", lambda conn, source_ids, limit: rows)
    monkeypatch.setattr(digest_job, "digest_article", lambda conn, settings, row, author: {"ok": True})
    # load_sources path uses real file via Settings; enabled_sources needs real yaml — ok via SOURCES_FILE
    client = _client(tmp_path)
    client.post("/api/v1/digest", json={"limit": 2})
    snapshot = _wait_stopped(digest_job)
    assert snapshot["completed_this_run"] == 2
    assert snapshot["last_id"] == 12
    assert snapshot["last_status"] == "ok"


def test_digest_failure_skips(tmp_path, isolated_jobs, monkeypatch):
    rows = [
        {"id": 21, "title": "坏", "source_id": "fangwei-mp"},
        {"id": 22, "title": "好", "source_id": "fangwei-mp"},
    ]

    def fake_digest(conn, settings, row, author):
        if int(row["id"]) == 21:
            raise LLMError("摘要为空")
        return {}

    monkeypatch.setattr(digest_job, "pending_articles", lambda conn, source_ids, limit: rows)
    monkeypatch.setattr(digest_job, "digest_article", fake_digest)
    monkeypatch.setattr(digest_job, "note_failure", lambda *a, **k: None)
    client = _client(tmp_path)
    client.post("/api/v1/digest", json={"limit": 2})
    snapshot = _wait_stopped(digest_job)
    assert snapshot["failed_this_run"] == 1
    assert snapshot["completed_this_run"] == 1


def test_views_gap_flag_and_origin(tmp_path, isolated_jobs, monkeypatch):
    monkeypatch.setattr(views_job, "view_companies", lambda conn, source_ids: [{"id": 1}])
    monkeypatch.setattr(views_job, "gap_articles", lambda conn, company_ids, source_ids, limit: [])
    def _pending_should_not_run(*_a, **_k):
        raise AssertionError("不应走 pending")

    monkeypatch.setattr(views_job, "pending_articles", _pending_should_not_run)
    client = _client(tmp_path)

    blocked = client.post("/api/v1/views", json={"gap": True}, headers={"origin": "https://evil.example"})
    assert blocked.status_code == 403

    started = client.post("/api/v1/views", json={"gap": True, "limit": 5, "loop": True, "sleep": 5})
    assert started.status_code == 200
    assert started.json()["gap"] is True
    client.post("/api/v1/views/stop")
    _wait_stopped(views_job)


def test_theses_force_and_missing_credentials(tmp_path, isolated_jobs, monkeypatch):
    monkeypatch.setattr(theses_job, "credentials_problem", lambda settings: "没有配置 DEEPSEEK_API_KEY，任务起不来。")
    client = _client(tmp_path)
    response = client.post("/api/v1/theses", json={"force": True})
    assert response.status_code == 409
    assert "DEEPSEEK_API_KEY" in response.json()["detail"]
    assert theses_job.status()["running"] is False

    monkeypatch.setattr(theses_job, "credentials_problem", lambda settings: "")
    monkeypatch.setattr(theses_job, "companies_with_views", lambda conn, source_ids, minimum=4: [])
    started = client.post("/api/v1/theses", json={"force": True, "loop": False})
    assert started.status_code == 200
    assert started.json()["force"] is True
    _wait_stopped(theses_job)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/digest",
        "/api/v1/digest/stop",
        "/api/v1/views",
        "/api/v1/views/stop",
        "/api/v1/theses",
        "/api/v1/theses/stop",
        "/api/v1/translate",
        "/api/v1/translate/stop",
    ],
)
@pytest.mark.parametrize("header", ["cf-connecting-ip", "cf-ray", "x-forwarded-for"])
def test_batch_jobs_refuse_requests_through_the_tunnel(tmp_path, isolated_jobs, path, header):
    """站点公开以后，经 Cloudflare 进来的请求不能启动或停止花钱的批量任务，没带 Origin 也不行。"""
    client = _client(tmp_path)
    response = client.post(path, json={"loop": True, "force": True}, headers={header: "203.0.113.9"})
    assert response.status_code == 403
    assert "本机" in response.json()["detail"]
    for mod in isolated_jobs:
        assert mod.status()["running"] is False


def test_theses_force_loop_rebuilds_once_then_waits(tmp_path, isolated_jobs, monkeypatch):
    """loop 加 force：第一轮全部重算，之后只算过期的，没有就休眠，不会一轮接一轮地重算。"""
    built: list[int] = []
    monkeypatch.setattr(
        theses_job, "companies_with_views", lambda conn, source_ids, minimum=4: [{"id": 1, "name_zh": "甲"}]
    )
    monkeypatch.setattr(theses_job, "needs_build", lambda conn, company_id, sources, settings: False)
    monkeypatch.setattr(theses_job, "build", lambda conn, settings, item, sources: built.append(item["id"]) or 1)
    client = _client(tmp_path)
    started = client.post("/api/v1/theses", json={"force": True, "loop": True, "sleep": 60})
    assert started.status_code == 200
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not theses_job.status().get("waiting"):
        time.sleep(0.01)
    assert theses_job.status()["waiting"] is True
    assert built == [1]
    client.post("/api/v1/theses/stop")
    _wait_stopped(theses_job)

"""站内译制接口：触发、单例、状态、停止、跨站拦截。不连数据库、不调真 LLM。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from researchlib.config import Settings
from researchlib.translate import worker
from researchlib.translate.service import TranslateResult
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


@pytest.fixture
def isolated_worker(monkeypatch):
    """每个测试一份干净的 worker 状态，结束时把线程停掉。"""
    monkeypatch.setattr(worker, "_thread", None)
    monkeypatch.setattr(worker, "_stop", threading.Event())
    monkeypatch.setattr(worker, "_state", worker._blank_state())
    worker._extra.clear()
    monkeypatch.setattr(worker, "llm_configured", lambda settings: True)
    monkeypatch.setattr(worker, "_connect", lambda settings: FakeConn())
    yield worker
    worker._stop.set()
    if worker._thread is not None:
        worker._thread.join(timeout=3)


def _client(tmp_path) -> TestClient:
    settings = Settings.from_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
            "DISPLAY_TIMEZONE": "Asia/Shanghai",
        }
    )
    app = FastAPI()
    app.state.settings = settings
    app.include_router(api)
    return TestClient(app)


def _wait_stopped(timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = worker.status()
        if not snapshot["running"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"worker 没停下：{worker.status()}")


def test_post_starts_one_worker_and_get_reports_status(tmp_path, isolated_worker, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-do-not-leak")
    monkeypatch.setattr(worker, "list_pending_english", lambda conn, *, limit, source_id=None: [])
    client = _client(tmp_path)

    first = client.post(
        "/api/v1/translate",
        json={"source": "stratechery", "limit": 10, "loop": True, "sleep": 5},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["started"] is True
    assert body["running"] is True
    assert body["source"] == "stratechery"
    assert body["limit"] == 10
    assert body["loop"] is True
    assert body["sleep"] == 5
    assert body["provider"] == "DeepSeek"
    assert body["started_at"]
    thread_ident = worker._thread.ident

    # 已在跑时再 POST 不起第二个
    second = client.post("/api/v1/translate", json={"source": "stratechery", "loop": True})
    assert second.status_code == 200
    assert second.json()["started"] is False
    assert second.json()["running"] is True
    assert second.json()["source"] == "stratechery"
    assert worker._thread.ident == thread_ident

    status = client.get("/api/v1/translate")
    assert status.status_code == 200
    snapshot = status.json()
    for key in (
        "running",
        "source",
        "limit",
        "loop",
        "sleep",
        "started_at",
        "finished_at",
        "completed_this_run",
        "failed_this_run",
        "last_article_id",
        "last_status",
        "last_error",
        "waiting",
        "stop_requested",
        "thread_alive",
    ):
        assert key in snapshot
    assert snapshot["running"] is True
    assert snapshot["thread_alive"] is True
    # 不回任何凭证
    assert "sk-test-do-not-leak" not in status.text
    assert "sk-test-do-not-leak" not in first.text

    stopped = client.post("/api/v1/translate/stop")
    assert stopped.status_code == 200
    assert stopped.json()["stopped"] is True
    final = _wait_stopped()
    assert final["running"] is False
    assert final["stop_requested"] is True
    assert final["finished_at"]


def test_finite_run_translates_pending_and_counts(tmp_path, isolated_worker, monkeypatch):
    seen: list[int] = []

    def fake_pending(conn, *, limit, source_id=None):
        assert source_id == "stratechery"
        return [21, 22]

    def fake_translate(conn, article_id, *, data_dir):
        seen.append(article_id)
        return TranslateResult(article_id, "translated", segments=2)

    monkeypatch.setattr(worker, "list_pending_english", fake_pending)
    monkeypatch.setattr(worker, "translate_article", fake_translate)
    client = _client(tmp_path)

    started = client.post("/api/v1/translate", json={"source": "stratechery", "limit": 2})
    assert started.status_code == 200
    assert started.json()["loop"] is False

    snapshot = _wait_stopped()
    assert seen == [21, 22]
    assert snapshot["completed_this_run"] == 2
    assert snapshot["failed_this_run"] == 0
    assert snapshot["last_article_id"] == 22
    assert snapshot["last_status"] == "translated"
    assert snapshot["last_error"] is None


def test_failed_article_is_skipped_within_run(tmp_path, isolated_worker, monkeypatch):
    seen: list[int] = []

    def fake_pending(conn, *, limit, source_id=None):
        return [31, 32]

    def fake_translate(conn, article_id, *, data_dir):
        seen.append(article_id)
        if article_id == 31:
            raise RuntimeError("对齐失败")
        return TranslateResult(article_id, "translated", segments=1)

    monkeypatch.setattr(worker, "list_pending_english", fake_pending)
    monkeypatch.setattr(worker, "translate_article", fake_translate)
    client = _client(tmp_path)

    client.post("/api/v1/translate", json={"limit": 2})
    snapshot = _wait_stopped()
    assert seen == [31, 32]
    assert snapshot["failed_this_run"] == 1
    assert snapshot["completed_this_run"] == 1
    assert snapshot["last_article_id"] == 32
    assert snapshot["last_status"] == "translated"


def test_error_message_is_reported_for_lone_failure(tmp_path, isolated_worker, monkeypatch):
    monkeypatch.setattr(worker, "list_pending_english", lambda conn, *, limit, source_id=None: [41])

    def fake_translate(conn, article_id, *, data_dir):
        raise RuntimeError("translation alignment: count")

    monkeypatch.setattr(worker, "translate_article", fake_translate)
    client = _client(tmp_path)

    client.post("/api/v1/translate", json={"limit": 1})
    snapshot = _wait_stopped()
    assert snapshot["failed_this_run"] == 1
    assert snapshot["last_article_id"] == 41
    assert snapshot["last_status"] == "failed"
    assert "translation alignment: count" in (snapshot["last_error"] or "")


def test_loop_skips_failed_ids_then_sleeps(tmp_path, isolated_worker, monkeypatch):
    """失败过的 id 在同一轮里只碰一次，队列空了先睡，不空转。"""
    calls: list[int] = []

    def fake_pending(conn, *, limit, source_id=None):
        return [51]

    def fake_translate(conn, article_id, *, data_dir):
        calls.append(article_id)
        raise RuntimeError("每篇都失败")

    monkeypatch.setattr(worker, "list_pending_english", fake_pending)
    monkeypatch.setattr(worker, "translate_article", fake_translate)
    client = _client(tmp_path)

    client.post("/api/v1/translate", json={"limit": 1, "loop": True, "sleep": 30})
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and worker.status()["waiting"] is False:
        time.sleep(0.01)
    assert worker.status()["waiting"] is True
    # 已经从「取队列 → 失败 → 再取队列」进入休眠，51 没有被连续磕第二次
    assert calls == [51]

    client.post("/api/v1/translate/stop")
    _wait_stopped()


def test_post_with_wrong_origin_is_forbidden(tmp_path, isolated_worker, monkeypatch):
    monkeypatch.setattr(worker, "list_pending_english", lambda conn, *, limit, source_id=None: [])
    client = _client(tmp_path)

    blocked = client.post(
        "/api/v1/translate",
        json={"source": "stratechery"},
        headers={"origin": "https://evil.example"},
    )
    assert blocked.status_code == 403
    assert worker.status()["running"] is False

    blocked_stop = client.post("/api/v1/translate/stop", headers={"origin": "https://evil.example"})
    assert blocked_stop.status_code == 403
    assert worker.status()["running"] is False

    # 本站来源放行
    allowed = client.post(
        "/api/v1/translate",
        json={"source": "stratechery", "loop": True, "sleep": 5},
        headers={"origin": "https://investment.zldong.com"},
    )
    assert allowed.status_code == 200
    client.post("/api/v1/translate/stop", headers={"origin": "https://investment.zldong.com"})
    _wait_stopped()


def test_recheck_translates_coarse_rows_not_the_pending_queue(tmp_path, isolated_worker, monkeypatch):
    seen: list[int] = []

    def fake_coarse(conn, *, limit, source_id=None):
        assert source_id == "stratechery"
        return [{"id": 21118, "source": "stratechery", "reason": "segments-below-leaves"}]

    def fake_pending(conn, *, limit, source_id=None):
        raise AssertionError("recheck 不该去待译队列")

    def fake_translate(conn, article_id, *, data_dir):
        seen.append(article_id)
        return TranslateResult(article_id, "translated", segments=40)

    monkeypatch.setattr(worker, "list_coarse_translations", fake_coarse)
    monkeypatch.setattr(worker, "list_pending_english", fake_pending)
    monkeypatch.setattr(worker, "translate_article", fake_translate)
    client = _client(tmp_path)

    started = client.post("/api/v1/translate", json={"source": "stratechery", "recheck": True, "limit": 5})
    assert started.status_code == 200
    assert started.json()["recheck"] is True
    snapshot = _wait_stopped()
    assert seen == [21118]
    assert snapshot["completed_this_run"] == 1
    assert snapshot["last_status"] == "translated"


def test_recheck_dry_run_lists_ids_without_translating(tmp_path, isolated_worker, monkeypatch):
    def fake_coarse(conn, *, limit, source_id=None):
        assert source_id is None
        assert limit == 100
        return [
            {"id": 21118, "source": "stratechery", "reason": "segments-below-source"},
            {"id": 21133, "source": "stratechery", "reason": "giant-leaf"},
        ]

    def fake_translate(conn, article_id, *, data_dir):
        raise AssertionError("dry_run 不调模型")

    monkeypatch.setattr(worker, "list_coarse_translations", fake_coarse)
    monkeypatch.setattr(worker, "translate_article", fake_translate)
    client = _client(tmp_path)

    response = client.post("/api/v1/translate", json={"recheck": True, "dry_run": True})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["count"] == 2
    assert [item["id"] for item in body["articles"]] == [21118, 21133]
    assert worker.status()["running"] is False

    missing = client.post("/api/v1/translate", json={"dry_run": True})
    assert missing.status_code == 400


def test_second_start_queues_named_articles(tmp_path, isolated_worker, monkeypatch):
    """导入时译制已在跑，新文章排进同一条线程，不丢。"""
    started = threading.Event()
    release = threading.Event()
    seen: list[int] = []

    def fake_pending(conn, *, limit, source_id=None):
        return []

    def fake_translate(conn, article_id, *, data_dir):
        seen.append(article_id)
        if article_id == 1:
            started.set()
            assert release.wait(timeout=3)
        return TranslateResult(article_id, "translated", segments=1)

    monkeypatch.setattr(worker, "list_pending_english", fake_pending)
    monkeypatch.setattr(worker, "translate_article", fake_translate)
    settings = _client(tmp_path).app.state.settings
    worker.start(settings, article_ids=[1], limit=5)
    assert started.wait(timeout=3)
    queued = worker.start(settings, article_ids=[2, 3], limit=5)
    assert queued["started"] is False
    assert queued["queued"] == 2
    release.set()
    snapshot = _wait_stopped()
    assert seen == [1, 2, 3]
    assert snapshot["completed_this_run"] == 3


def test_missing_credentials_is_a_409_not_a_thread(tmp_path, isolated_worker, monkeypatch):
    monkeypatch.setattr(worker, "llm_configured", lambda settings: False)
    client = _client(tmp_path)

    response = client.post("/api/v1/translate", json={"source": "stratechery"})
    assert response.status_code == 409
    assert "DEEPSEEK_API_KEY" in response.json()["detail"]
    assert worker.status()["running"] is False

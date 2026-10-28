"""识图组包、失败不回退、API 单例。不连库、不调真 DeepSeek。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from researchlib.config import Settings
from researchlib.jobs import ocr as ocr_job
from researchlib.llm import LLMError
from researchlib.ocr import (
    VISION_PROMPT,
    OcrUnavailable,
    normalize_vision_text,
    resolve_engine,
    run_ocr,
    vision_engine,
    vision_engine_id,
    vision_messages,
)
from researchlib.web.api import api

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-ocr-vision-must-not-leak"


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
            "AIHOT_MAP_FILE": str(ROOT / "config" / "aihot_import.yaml"),
            "DISPLAY_TIMEZONE": "Asia/Shanghai",
            "DEEPSEEK_API_KEY": SECRET,
        }
    )


def test_vision_prompt_names_the_constraints():
    for phrase in (
        "绿色气泡",
        "方伟：",
        "网友：",
        "Aaron",
        "Henri",
        "Kevin",
        "可按住转文字",
        "回到引用位置",
        "设为群待办",
        "继续提问",
        "AI搜索",
        "状态栏时间",
        "群名",
        "雪球",
        "LIVE",
        "加人会员群",
        "小程序",
        "一一对应",
        "?",
        "禁止编造",
        "不要额外评论",
    ):
        assert phrase in VISION_PROMPT, phrase


def test_default_engine_id_is_deepseek_flash_vision():
    assert vision_engine_id() == "deepseek-flash-vision"


def test_vision_messages_are_text_plus_image_url():
    messages = vision_messages(b"\x89PNG\r\n", "image/png")
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert content[0]["text"] == VISION_PROMPT
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_recognize_posts_multimodal_messages(tmp_path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    seen = {}

    def fake_chat(settings, messages, **kwargs):
        seen["messages"] = messages
        return "```\n方伟：底线\n网友：非共识\n```", "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    text = vision_engine(_settings(tmp_path))(image)
    assert text == "方伟：底线\n网友：非共识"
    assert normalize_vision_text("```text\n方伟：底线\n```") == "方伟：底线"
    content = seen["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_without_key_does_not_fall_back(monkeypatch):
    monkeypatch.setattr("researchlib.llm.llm_configured", lambda settings: False)
    with pytest.raises(OcrUnavailable, match="rapidocr"):
        resolve_engine(object(), "vision")


def test_http_error_is_explicit_empty_not_dirty_text(tmp_path, monkeypatch):
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    (image_dir / "a.png").write_bytes(b"png")
    monkeypatch.setattr(
        "researchlib.ocr.pending_images",
        lambda *args, **kwargs: [{"content_hash": "ab" * 32, "storage_path": "a.png"}],
    )

    def boom(_path):
        raise LLMError("DeepSeek 接口返回 500：busy")

    class Conn:
        def __init__(self):
            self.writes = []

        def execute(self, sql, params=None):
            compact = " ".join(sql.split())
            if compact.startswith("INSERT INTO image_texts"):
                self.writes.append(params)

            class Result:
                def fetchall(self_inner):
                    return []

                def fetchone(self_inner):
                    return None

            return Result()

        def commit(self):
            return None

    conn = Conn()
    report = run_ocr(conn, tmp_path, 1, boom, engine="deepseek-flash-vision")
    assert report["failed"] == 1
    assert report["with_text"] == 0
    assert conn.writes[0][1] == "deepseek-flash-vision:error"
    assert conn.writes[0][2] == ""


def test_nightly_uses_app_vision_not_ocr_profile():
    text = (ROOT / "scripts" / "nightly.sh").read_text()
    assert "--profile ocr" not in text
    assert "research-ocr --engine vision" in text


class FakeConn:
    def close(self):
        return None

    def execute(self, *args, **kwargs):
        class Result:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None

        return Result()


@pytest.fixture
def isolated_ocr(monkeypatch):
    monkeypatch.setattr(ocr_job._worker, "_thread", None)
    monkeypatch.setattr(ocr_job._worker, "_stop", threading.Event())
    monkeypatch.setattr(ocr_job._worker, "_state", ocr_job._worker._blank())
    monkeypatch.setattr(ocr_job, "connect", lambda settings: FakeConn())
    monkeypatch.setattr(ocr_job, "credentials_problem", lambda settings, engine=None: "")
    yield ocr_job
    ocr_job._worker.stop_event.set()
    if ocr_job._worker._thread is not None:
        ocr_job._worker._thread.join(timeout=3)


def _client(tmp_path) -> TestClient:
    app = FastAPI()
    app.state.settings = _settings(tmp_path)
    app.include_router(api)
    return TestClient(app)


def _wait_stopped(timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = ocr_job.status()
        if not snapshot["running"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"识图 worker 没停下：{ocr_job.status()}")


def test_ocr_api_does_not_start_a_second_worker(tmp_path, isolated_ocr, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        started.set()
        while not release.is_set() and not ocr_job._worker.stop_event.is_set():
            release.wait(0.05)
        return {"images": 1, "with_text": 1, "failed": 0, "missing": 0, "articles": 1, "last_error": None}

    monkeypatch.setattr("researchlib.ocr.pending_images", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "researchlib.ocr.resolve_engine",
        lambda settings, choice: ("deepseek-flash-vision", lambda path: ""),
    )
    monkeypatch.setattr("researchlib.ocr.run_ocr", fake_run)
    client = _client(tmp_path)

    first = client.post(
        "/api/v1/ocr",
        json={"articles": [24002, 24007], "redo": True, "limit": 50, "engine": "vision"},
    )
    assert first.status_code == 200
    assert first.json()["started"] is True
    assert first.json()["running"] is True
    assert SECRET not in first.text
    assert started.wait(timeout=2)

    second = client.post("/api/v1/ocr", json={"articles": [1], "redo": False})
    assert second.status_code == 200
    assert second.json()["started"] is False
    assert second.json()["running"] is True
    assert SECRET not in second.text

    status = client.get("/api/v1/ocr")
    assert status.status_code == 200
    body = status.json()
    for key in ("running", "engine", "redo", "images", "failed", "last_error", "thread_alive"):
        assert key in body
    assert body["running"] is True
    assert SECRET not in status.text
    assert "sk-" not in status.text

    stopped = client.post("/api/v1/ocr/stop")
    assert stopped.status_code == 200
    assert stopped.json()["stopped"] is True
    release.set()
    final = _wait_stopped()
    assert final["running"] is False
    assert len(calls) == 1
    assert calls[0]["redo"] is True
    assert calls[0]["article_ids"] == [24002, 24007]
    assert calls[0]["engine"] == "deepseek-flash-vision"


def test_ocr_api_rejects_cross_origin_and_missing_key(tmp_path, isolated_ocr, monkeypatch):
    client = _client(tmp_path)
    blocked = client.post("/api/v1/ocr", json={"limit": 1}, headers={"origin": "https://evil.example"})
    assert blocked.status_code == 403
    blocked_stop = client.post("/api/v1/ocr/stop", headers={"origin": "https://evil.example"})
    assert blocked_stop.status_code == 403

    def missing(_settings, engine=None):
        return "没有配置 DEEPSEEK_API_KEY，识图起不来。"

    monkeypatch.setattr(ocr_job, "credentials_problem", missing)
    denied = client.post("/api/v1/ocr", json={"engine": "vision"})
    assert denied.status_code == 409
    assert "DEEPSEEK_API_KEY" in denied.json()["detail"]
    assert SECRET not in denied.text
    assert ocr_job.status()["running"] is False


def test_llms_txt_documents_ocr_endpoint():
    """llms.txt 的文案写在 api.py 里，真正渲染要连库，集成测才打 GET。"""
    api_src = (ROOT / "src" / "researchlib" / "web" / "api.py").read_text(encoding="utf-8")
    assert "/api/v1/ocr" in api_src
    assert "触发图片文字" in api_src

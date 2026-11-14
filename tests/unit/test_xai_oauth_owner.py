"""Shared AIHOT token path / ownership helpers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from researchlib.config import Settings
from researchlib.llm import credential_kind
from researchlib.translate.xai_oauth import (
    XaiLoginRequiredError,
    _desired_owner,
    _is_foreign,
    _write_secret,
    get_xai_access_token,
    is_stale_xai_access,
    xai_oauth_status,
    xai_token_path,
)


def test_desired_owner_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XAI_OAUTH_FILE_UID", "1000")
    monkeypatch.setenv("XAI_OAUTH_FILE_GID", "1000")
    assert _desired_owner(tmp_path / "xai-oauth.json") == (1000, 1000)


def test_desired_owner_from_existing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XAI_OAUTH_FILE_UID", raising=False)
    monkeypatch.delenv("XAI_OAUTH_FILE_GID", raising=False)
    target = tmp_path / "xai-oauth.json"
    target.write_text("{}", encoding="utf-8")
    st = target.stat()
    assert _desired_owner(target) == (st.st_uid, st.st_gid)


def test_aihot_v1_token_is_not_foreign() -> None:
    assert _is_foreign(
        {
            "version": 1,
            "access_token": "a",
            "refresh_token": "r",
            "token_type": "Bearer",
            "expires_at": 1,
            "obtained_at": "1970-01-01T00:00:00Z",
            "needs_login": False,
            "last_error": None,
        }
    ) is False


def test_hermes_style_is_foreign() -> None:
    assert _is_foreign({"providers": {"xai": {}}, "access_token": "x"}) is True


def test_default_token_path_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", "/aihot-data/secrets/xai-oauth.json")
    assert xai_token_path() == Path("/aihot-data/secrets/xai-oauth.json")


def test_write_secret_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XAI_OAUTH_FILE_UID", str(os.getuid()))
    monkeypatch.setenv("XAI_OAUTH_FILE_GID", str(os.getgid()))
    path = tmp_path / "secrets" / "xai-oauth.json"
    payload = {"version": 1, "access_token": "tok", "refresh_token": "ref"}
    _write_secret(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def _aihot_token(path: Path, *, expires_at: float, access: str = "old-access") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": access,
                "refresh_token": "old-refresh",
                "token_type": "Bearer",
                "expires_at": expires_at,
                "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
                "needs_login": False,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )


def test_shared_v1_token_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "xai-oauth.json"
    _aihot_token(path, expires_at=time.time() * 1000 + 3_600_000)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(path))
    assert get_xai_access_token(tmp_path) == "old-access"
    assert xai_oauth_status(tmp_path).logged_in is True
    assert xai_oauth_status(tmp_path).token_path == str(path)
    monkeypatch.setenv("XAI_API_KEY", "console-key-ignored")
    settings = Settings.from_env(
        {"DATA_DIR": str(tmp_path), "RESEARCH_LLM_PROVIDER": "grok", "XAI_API_KEY": "console-key-ignored"}
    )
    assert credential_kind(settings) == "oauth"
    assert not hasattr(settings, "llm_api_key")


def test_hermes_file_is_not_overwritten(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    original = json.dumps({"providers": {"xai": {"refresh_token": "theirs"}}})
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(path))
    with pytest.raises(XaiLoginRequiredError, match="拒绝写入"):
        get_xai_access_token(tmp_path)
    assert path.read_text(encoding="utf-8") == original


def test_refresh_writes_aihot_v1_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "xai-oauth.json"
    _aihot_token(path, expires_at=time.time() * 1000 - 10_000)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(path))
    monkeypatch.setenv("XAI_OAUTH_FILE_UID", str(os.getuid()))
    monkeypatch.setenv("XAI_OAUTH_FILE_GID", str(os.getgid()))
    calls: list[dict] = []

    def fake_post(url, fields):
        calls.append(fields)
        return 200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900}

    monkeypatch.setattr("researchlib.translate.xai_oauth._endpoints", lambda: ("https://x/device", "https://x/token"))
    monkeypatch.setattr("researchlib.translate.xai_oauth._post_form", fake_post)
    assert get_xai_access_token(tmp_path) == "new-access"
    assert calls[0]["refresh_token"] == "old-refresh"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["refresh_token"] == "new-refresh"
    assert saved["access_token"] == "new-access"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert get_xai_access_token(tmp_path) == "new-access"
    assert len(calls) == 1


def test_terminal_refresh_marks_shared_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "xai-oauth.json"
    _aihot_token(path, expires_at=0)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(path))
    monkeypatch.setenv("XAI_OAUTH_FILE_UID", str(os.getuid()))
    monkeypatch.setenv("XAI_OAUTH_FILE_GID", str(os.getgid()))
    monkeypatch.setattr("researchlib.translate.xai_oauth._endpoints", lambda: ("https://x/device", "https://x/token"))
    monkeypatch.setattr(
        "researchlib.translate.xai_oauth._post_form",
        lambda url, fields: (400, {"error": "invalid_grant"}),
    )
    with pytest.raises(XaiLoginRequiredError):
        get_xai_access_token(tmp_path)
    status = xai_oauth_status(tmp_path)
    assert status.needs_login is True
    assert status.logged_in is False


def test_stale_access_detection() -> None:
    assert is_stale_xai_access(401, "")
    assert is_stale_xai_access(403, "OAuth2 access token could not be validated")
    assert not is_stale_xai_access(403, "subscription required")


def _expired_token(path: Path, refresh: str = "r-old") -> None:
    _write_secret(
        path,
        {
            "version": 1,
            "access_token": "a-old",
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_at": 1,
            "obtained_at": "1970-01-01T00:00:00Z",
            "needs_login": False,
            "last_error": None,
        },
    )


def test_two_processes_refreshing_at_once_use_one_refresh_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """刷新慢（auth.x.ai 慢一次）时，后到的等锁，拿前一个换好的凭证；不再拿同一个 refresh token 再换一次，
    收到「已用过」把刚换好的凭证清成需要重新登录。"""
    import threading

    from researchlib.translate import xai_oauth

    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "xai-oauth.json"))
    path = xai_token_path(tmp_path)
    _expired_token(path)
    used: list[str] = []

    def slow_refresh(state):
        used.append(state["refresh_token"])
        if state["refresh_token"] != "r-old":
            raise AssertionError("不该再刷新")
        time.sleep(0.3)
        return {
            "version": 1,
            "access_token": "a-new",
            "refresh_token": "r-new",
            "token_type": "Bearer",
            "expires_at": time.time() * 1000 + 3_600_000,
            "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "needs_login": False,
            "last_error": None,
        }

    monkeypatch.setattr(xai_oauth, "_refresh", slow_refresh)
    got: list[str] = []
    threads = [threading.Thread(target=lambda: got.append(get_xai_access_token(tmp_path))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert got == ["a-new"] * 3
    assert used == ["r-old"]
    assert json.loads(path.read_text())["needs_login"] is False


def test_refresh_token_already_used_falls_back_to_what_another_process_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from researchlib.translate import xai_oauth

    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "xai-oauth.json"))
    path = xai_token_path(tmp_path)
    _expired_token(path)

    def used_elsewhere(state):
        # 锁外的另一个进程（另一个容器）抢先换好了，写进了文件。
        fresh = dict(state, access_token="a-other", refresh_token="r-other", expires_at=time.time() * 1000 + 3_600_000)
        _write_secret(path, fresh)
        raise XaiLoginRequiredError("xAI OAuth 需要重新登录：HTTP 400 invalid_grant: refresh token already been used")

    monkeypatch.setattr(xai_oauth, "_refresh", used_elsewhere)
    assert get_xai_access_token(tmp_path) == "a-other"
    assert json.loads(path.read_text())["needs_login"] is False


def test_refresh_that_cannot_connect_is_retryable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from researchlib.translate import xai_oauth

    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "xai-oauth.json"))
    path = xai_token_path(tmp_path)
    _expired_token(path)

    def down(state):
        raise OSError("连不上")

    monkeypatch.setattr(xai_oauth, "_refresh", down)
    with pytest.raises(xai_oauth.XaiRefreshUnavailable):
        get_xai_access_token(tmp_path)
    assert json.loads(path.read_text())["needs_login"] is False

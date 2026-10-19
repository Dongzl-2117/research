"""Shared AIHOT token path / ownership helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from researchlib.translate.xai_oauth import _desired_owner, _is_foreign, _write_secret, xai_token_path


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

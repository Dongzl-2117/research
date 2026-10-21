"""问库和译制共用同一套 Grok 强度，而且只认 OAuth。有 key、没有登录时必须失败。"""

import json
import time

import pytest

from researchlib import grok, health
from researchlib import llm as llm_module
from researchlib.config import Settings
from researchlib.grok import (
    GROK_CONTEXT_TOKENS,
    GROK_MODEL_ID,
    GROK_OUTPUT_FLOOR,
    GROK_REASONING_EFFORT,
    completion_params,
    grok_output_tokens,
    long_timeout_s,
)
from researchlib.llm import LLMUnavailable, chat, credential_kind, ensure_oauth, llm_configured
from researchlib.translate import llm as translate_llm


def _write_oauth(path, access="oauth-access") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": access,
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_at": time.time() * 1000 + 3_600_000,
                "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
                "needs_login": False,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused/research_test",
            "DATA_DIR": str(tmp_path),
            "XAI_API_KEY": "should-not-be-used",
            "RESEARCH_LLM_MODEL": "grok-4-fast",
            "RESEARCH_LLM_REASONING": "low",
            "XAI_OAUTH_TOKEN_PATH": str(tmp_path / "missing-oauth.json"),
        }
    )


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "missing-oauth.json"))
    monkeypatch.setenv("XAI_API_KEY", "should-not-be-used")
    monkeypatch.setenv("RESEARCH_LLM_MODEL", "grok-4-fast")
    monkeypatch.setenv("RESEARCH_LLM_REASONING", "")
    monkeypatch.delenv("XAI_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_LONG_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("RESEARCH_LLM_TIMEOUT", raising=False)


def test_api_key_without_oauth_fails(tmp_path, monkeypatch):
    health.reset_llm_cache()
    settings = _settings(tmp_path)
    assert credential_kind(settings) == ""
    assert llm_configured(settings) is False
    assert not hasattr(settings, "llm_api_key")
    assert "should-not-be-used" not in repr(settings)
    called = []
    monkeypatch.setattr(llm_module, "_post", lambda *_args, **_kwargs: called.append(1))
    with pytest.raises(LLMUnavailable, match="不会被使用") as exc:
        chat(settings, [{"role": "user", "content": "hi"}], max_tokens=800)
    assert called == []
    assert "should-not-be-used" not in str(exc.value)
    with pytest.raises(LLMUnavailable, match="OAuth"):
        ensure_oauth(settings)
    health.reset_llm_cache()


def test_shared_constants_are_the_translation_ones():
    assert translate_llm.GROK_MODEL_ID is GROK_MODEL_ID == "grok-4.7"
    assert translate_llm.GROK_REASONING_EFFORT is GROK_REASONING_EFFORT == "xhigh"
    assert translate_llm.GROK_CONTEXT_TOKENS is GROK_CONTEXT_TOKENS == 500_000
    assert translate_llm.GROK_OUTPUT_FLOOR is GROK_OUTPUT_FLOOR == 128_000
    assert translate_llm.grok_output_tokens is grok_output_tokens
    assert translate_llm.completion_params is completion_params
    assert "fast" not in GROK_MODEL_ID


def test_output_floor_cannot_be_lowered_by_env(monkeypatch):
    assert grok_output_tokens(800) == GROK_OUTPUT_FLOOR
    monkeypatch.setenv("XAI_MAX_TOKENS", "64000")
    assert grok_output_tokens(800) == GROK_OUTPUT_FLOOR
    monkeypatch.setenv("XAI_MAX_TOKENS", "200000")
    assert grok_output_tokens(800) == 200_000
    assert grok_output_tokens(300_000) == 300_000
    monkeypatch.delenv("XAI_MAX_TOKENS")
    monkeypatch.setenv("LLM_MAX_TOKENS", "1000")
    assert grok_output_tokens(None) == GROK_OUTPUT_FLOOR


def test_timeout_cannot_be_shorter_than_translation():
    assert long_timeout_s({}) == 1_500
    assert long_timeout_s({"RESEARCH_LLM_TIMEOUT": "90"}) == 1_500
    assert long_timeout_s({"LLM_LONG_TIMEOUT_MS": "2000000"}) == 2_000
    assert long_timeout_s({"LLM_LONG_TIMEOUT_MS": "2000000", "RESEARCH_LLM_TIMEOUT": "100"}) == 2_000
    assert long_timeout_s({"LLM_LONG_TIMEOUT_MS": "2000000", "RESEARCH_LLM_TIMEOUT": "2500"}) == 2_500
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused/research_test",
            "DATA_DIR": "/tmp/research-grok-strength",
            "RESEARCH_LLM_TIMEOUT": "900",
        }
    )
    assert settings.llm_timeout == 1_500


def test_chat_payload_matches_translation_strength(tmp_path, monkeypatch):
    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token, access="oauth-access")
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    monkeypatch.setenv("XAI_MAX_TOKENS", "64000")
    seen = {}

    def fake_post(_settings, bearer, payload):
        seen["bearer"] = bearer
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "好"}}], "model": "grok-4-fast"}

    monkeypatch.setattr(llm_module, "_post", fake_post)
    health.reset_llm_cache()
    text, model = chat(
        _settings(tmp_path),
        [{"role": "user", "content": "hi"}],
        max_tokens=800,
        temperature=0.1,
    )
    assert (text, model) == ("好", "grok-4-fast")
    payload = seen["payload"]
    assert seen["bearer"] == "oauth-access"
    assert payload["model"] == "grok-4.7"
    assert payload["reasoning_effort"] == "xhigh"
    assert payload["max_tokens"] >= 128_000
    assert payload["temperature"] == 0.1
    assert "fast" not in payload
    assert "fast" not in payload["model"]
    for key in ("context_length", "context_window", "max_context_tokens", "max_input_tokens"):
        assert key not in payload
    assert GROK_CONTEXT_TOKENS == 500_000
    health.reset_llm_cache()


def test_chat_json_uses_the_same_strength(tmp_path, monkeypatch):
    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token, access="oauth-access")
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    monkeypatch.setenv("XAI_MAX_TOKENS", "1000")
    captured = {}

    class _Response:
        status = 200

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    result = translate_llm.chat_json(
        system="sys",
        user={"q": "hi"},
        purpose="translate_body",
        subject="batch",
        temperature=0.3,
        max_tokens=800,
        data_dir=tmp_path,
        timeout_s=12,
    )
    body = captured["body"]
    assert result.data == {"ok": True}
    assert result.model == "grok-4.7"
    assert captured["auth"] == "Bearer oauth-access"
    assert "should-not-be-used" not in captured["auth"]
    assert body["model"] == completion_params(temperature=0.3, max_tokens=800)["model"]
    assert body["reasoning_effort"] == "xhigh"
    assert body["max_tokens"] == grok.GROK_OUTPUT_FLOOR
    assert body["response_format"] == {"type": "json_object"}
    assert "fast" not in body
    assert captured["timeout"] == 12

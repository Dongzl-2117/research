"""站内 LLM 默认 DeepSeek：deepseek-flash / thinking / max / 393216 起；Grok 保留为显式选项。"""

import json
import time

import pytest

from researchlib import deepseek, health
from researchlib import llm as llm_module
from researchlib.config import Settings
from researchlib.llm import (
    LLMError,
    LLMUnavailable,
    active_label,
    active_model_id,
    chat,
    credential_kind,
    ensure_oauth,
    llm_configured,
)
from researchlib.llm_provider import llm_provider
from researchlib.translate import llm as translate_llm


class _Response:
    status = 200

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCH_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("DEEPSEEK_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "missing-oauth.json"))
    health.reset_llm_cache()
    yield
    health.reset_llm_cache()


def _settings(tmp_path, **extra) -> Settings:
    return Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused/research_test",
            "DATA_DIR": str(tmp_path),
            **extra,
        }
    )


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


def test_provider_defaults_to_deepseek():
    assert llm_provider({}) == "deepseek"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": ""}) == "deepseek"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": " DeepSeek "}) == "deepseek"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "GROK"}) == "grok"
    # 填了不认识的值也按默认来，不因为一个拼写错误把站点打挂。
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "xai"}) == "deepseek"


def test_settings_defaults_are_deepseek(tmp_path):
    settings = _settings(tmp_path)
    assert settings.llm_provider == "deepseek"
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert active_label(settings) == "DeepSeek"
    assert active_model_id(settings) == deepseek.DEEPSEEK_MODEL_ID == "deepseek-flash"
    assert credential_kind(settings) == ""
    assert llm_configured(settings) is False


def test_deepseek_chat_uses_official_endpoint_and_max_strength(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _Response(
            {
                "choices": [{"message": {"content": " 好 ", "reasoning_content": "先想一下"}}],
                "model": "deepseek-flash",
            }
        )

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    settings = _settings(tmp_path)
    assert credential_kind(settings) == "deepseek"
    assert llm_configured(settings) is True

    text, model = chat(
        settings,
        [{"role": "user", "content": "hi"}],
        max_tokens=800,
        temperature=0.1,
    )

    assert (text, model) == ("好", "deepseek-flash")
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["auth"] == "Bearer sk-test-not-real"
    body = captured["body"]
    assert body["model"] == "deepseek-flash"
    assert body["reasoning_effort"] == "max"
    assert body["thinking"] == {"type": "enabled"}
    assert body["max_tokens"] == deepseek.DEEPSEEK_OUTPUT_FLOOR == 393_216
    assert "temperature" not in body
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_deepseek_chat_rejects_empty_content(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")

    def fake_urlopen(req, timeout=None):
        return _Response(
            {
                "choices": [{"message": {"content": "  ", "reasoning_content": "只想了不想答"}}],
                "model": "deepseek-flash",
            }
        )

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    settings = _settings(tmp_path)
    with pytest.raises(LLMError, match="正文为空"):
        chat(settings, [{"role": "user", "content": "hi"}])


def test_deepseek_chat_accepts_list_content(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")

    def fake_urlopen(req, timeout=None):
        return _Response(
            {
                "choices": [{"message": {"content": [{"type": "text", "text": "分段答"}]}}],
                "model": "deepseek-flash",
            }
        )

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    settings = _settings(tmp_path)
    text, model = chat(settings, [{"role": "user", "content": "hi"}])
    assert (text, model) == ("分段答", "deepseek-flash")


def test_deepseek_without_key_fails_without_http(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(llm_module, "_post", lambda *_args, **_kwargs: called.append(1))
    settings = _settings(tmp_path)
    with pytest.raises(LLMUnavailable, match="DEEPSEEK_API_KEY"):
        chat(settings, [{"role": "user", "content": "hi"}])
    assert called == []
    with pytest.raises(LLMUnavailable, match="DEEPSEEK_API_KEY"):
        ensure_oauth(settings)
    state = health.read_status(tmp_path, "llm")
    assert state["unavailable"] is True and state["provider"] == "DeepSeek"


def _old_rejects_flash_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    with pytest.raises(RuntimeError, match="flash"):
        deepseek.completion_params()
    called = []
    monkeypatch.setattr(llm_module, "_post", lambda *_args, **_kwargs: called.append(1))
    with pytest.raises(RuntimeError, match="flash"):
        chat(_settings(tmp_path), [{"role": "user", "content": "hi"}])
    assert called == []


def test_deepseek_output_floor_and_reasoning_effort(monkeypatch):
    assert deepseek.deepseek_output_tokens(800) == deepseek.DEEPSEEK_OUTPUT_FLOOR
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "64000")
    assert deepseek.deepseek_output_tokens(800) == deepseek.DEEPSEEK_OUTPUT_FLOOR
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "400000")
    assert deepseek.deepseek_output_tokens(800) == 400_000
    monkeypatch.delenv("DEEPSEEK_MAX_TOKENS")
    monkeypatch.setenv("LLM_MAX_TOKENS", "1000")
    assert deepseek.deepseek_output_tokens(None) == deepseek.DEEPSEEK_OUTPUT_FLOOR

    assert deepseek.deepseek_reasoning_effort({}) == "max"
    assert deepseek.deepseek_reasoning_effort({"DEEPSEEK_REASONING_EFFORT": "HIGH"}) == "high"
    assert deepseek.deepseek_reasoning_effort({"DEEPSEEK_REASONING_EFFORT": "xhigh"}) == "high"
    assert deepseek.deepseek_reasoning_effort({"DEEPSEEK_REASONING_EFFORT": "turbo"}) == "max"


def test_translate_chat_json_uses_deepseek(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _Response(
            {
                "choices": [{"message": {"content": '{"t": ["你好"]}', "reasoning_content": "想"}}],
                "model": "deepseek-flash",
            }
        )

    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    result = translate_llm.chat_json(
        system="sys",
        user={"segments": ["hi"]},
        purpose="translate_body",
        subject="batch",
        temperature=0.3,
        max_tokens=800,
        data_dir=tmp_path,
        timeout_s=12,
    )

    assert result.data == {"t": ["你好"]}
    assert result.model == "deepseek-flash"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["auth"] == "Bearer sk-test-not-real"
    body = captured["body"]
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    assert body["max_tokens"] == deepseek.DEEPSEEK_OUTPUT_FLOOR
    assert body["response_format"] == {"type": "json_object"}
    assert "temperature" not in body
    assert captured["timeout"] == 12


def test_translate_without_deepseek_key_fails(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        translate_llm.chat_json(
            system="sys",
            user={"segments": ["hi"]},
            purpose="translate_body",
            subject="batch",
            data_dir=tmp_path,
        )


def test_grok_opt_in_still_uses_oauth(tmp_path, monkeypatch):
    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token)
    monkeypatch.setenv("RESEARCH_LLM_PROVIDER", "grok")
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    # 就算机器上有 DeepSeek key，grok 路径也不该用它。
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    seen = {}

    def fake_post(_settings, bearer, payload):
        seen["bearer"] = bearer
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "好"}}], "model": "grok-4.7"}

    monkeypatch.setattr(llm_module, "_post", fake_post)
    settings = _settings(tmp_path, RESEARCH_LLM_PROVIDER="grok")
    assert settings.llm_base_url == "https://api.x.ai/v1"
    assert credential_kind(settings) == "oauth"

    text, model = chat(
        settings,
        [{"role": "user", "content": "hi"}],
        max_tokens=800,
        temperature=0.1,
    )

    assert (text, model) == ("好", "grok-4.7")
    assert seen["bearer"] == "oauth-access"
    payload = seen["payload"]
    assert payload["model"] == "grok-4.7"
    assert payload["reasoning_effort"] == "xhigh"
    assert payload["max_tokens"] == 128_000
    assert payload["temperature"] == 0.1
    assert "thinking" not in payload


def test_deepseek_stream_is_reassembled():
    from researchlib.llm import _read_stream

    lines = [
        'data: {"model":"deepseek-flash","choices":[{"delta":{"reasoning_content":"想"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"茅台"}}]}',
        'data: {"choices":[{"delta":{"content":"很好"}}]}',
        "data: [DONE]",
    ]
    body = _read_stream(lines)
    assert body["model"] == "deepseek-flash"
    assert body["choices"][0]["message"]["content"] == "茅台很好"
    assert body["choices"][0]["message"]["reasoning_content"] == "想"

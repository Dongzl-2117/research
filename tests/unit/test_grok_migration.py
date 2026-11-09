"""2026-11-10 的迁移口径：默认 Grok OAuth，DeepSeek 用环境变量切回去。

这几条单独钉住：

- 默认提供方是 grok。``RESEARCH_LLM_PROVIDER=deepseek`` 走 DeepSeek，
  ``deepseek-legacy`` 是同一个值的别名。不认识的值按 grok。
- 当前提供方凭证不可用时直接报错，不去取另一家的凭证，也不发第二个请求。
- Grok 强度：grok-4.7 / reasoning_effort=high / fast=false / 50 万上下文 / 输出至少 128000。
- 并发：默认 8，``RESEARCH_LLM_CONCURRENCY`` 直接生效，没有低硬顶。
  只有显式的 ``GROK_MAX_CONCURRENCY`` 才往下夹。退避间隔可用 LLM_RETRY_DELAYS 调。
- 每一次 429 打 ``rate-limit event=429``，重试后成功的也记。
- 透传 on_text（问库边写边显示）时走流式，用量仍要报回来。
"""

import io
import json
import time
from email.message import EmailMessage

import pytest

from researchlib import grok, health
from researchlib import llm as llm_module
from researchlib.config import Settings
from researchlib.grok import (
    GROK_CONCURRENCY_DEFAULT,
    RETRY_DELAYS_DEFAULT,
    completion_params,
    grok_concurrency,
    grok_concurrency_plan,
    retry_delays,
)
from researchlib.llm import LLMUnavailable, chat, credential_kind, http_stats, reset_http_stats
from researchlib.llm_provider import llm_provider
from researchlib.translate import llm as translate_llm


def _settings(tmp_path, **extra) -> Settings:
    return Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused/research_test",
            "DATA_DIR": str(tmp_path),
            "XAI_OAUTH_TOKEN_PATH": str(tmp_path / "missing-oauth.json"),
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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    health.reset_llm_cache()
    for name in ("RESEARCH_LLM_PROVIDER", "DEEPSEEK_API_KEY", "XAI_MAX_TOKENS", "LLM_MAX_TOKENS", "LLM_RETRY_DELAYS"):
        monkeypatch.delenv(name, raising=False)
    yield
    health.reset_llm_cache()


def test_provider_switches_with_one_env_knob():
    """grok 和 deepseek 都是正式取值。改环境变量就换，不认识的值按默认 grok。"""
    assert llm_provider({}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": ""}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "GROK"}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "xai"}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "deepseek"}) == "deepseek"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": " DeepSeek "}) == "deepseek"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "deepseek-legacy"}) == "deepseek"


def test_no_silent_fallback_to_deepseek(tmp_path, monkeypatch):
    """OAuth 没登录、机器上又有 DeepSeek key 时：报错，不换提供方，不发第二个请求。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-never-be-used")
    settings = _settings(tmp_path)
    assert credential_kind(settings) == ""
    called = []
    monkeypatch.setattr(llm_module, "_deepseek_token", lambda: called.append("deepseek") or "sk-nope")
    monkeypatch.setattr(llm_module, "_post", lambda *_a, **_k: called.append("post"))
    with pytest.raises(LLMUnavailable):
        chat(settings, [{"role": "user", "content": "hi"}])
    assert called == []
    assert "sk-should-never-be-used" not in repr(settings)


def test_strength_is_high_not_xhigh_and_fast_is_explicitly_off():
    params = completion_params(temperature=0.2)
    assert params["model"] == "grok-4.7"
    assert params["reasoning_effort"] == "high"
    assert params["fast"] is False
    assert params["max_tokens"] >= 128_000
    assert grok.GROK_CONTEXT_TOKENS == 500_000  # /v1/models/grok-4.7 报的 context_length
    for key in ("context_length", "context_window", "max_context_tokens", "max_input_tokens"):
        assert key not in params


def test_concurrency_has_no_low_ceiling():
    assert grok_concurrency({}) == GROK_CONCURRENCY_DEFAULT == 8
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "16"}) == 16
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "64"}) == 64
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "0"}) == 1
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "abc"}) == 8
    # 只有显式设了 GROK_MAX_CONCURRENCY 才往下夹。填坏等于没设。
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "40", "GROK_MAX_CONCURRENCY": "16"}) == 16
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "4", "GROK_MAX_CONCURRENCY": "16"}) == 4
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "40", "GROK_MAX_CONCURRENCY": "nope"}) == 40
    assert grok_concurrency_plan({"RESEARCH_LLM_CONCURRENCY": "40"}) == {
        "asked": 40,
        "ceiling": None,
        "effective": 40,
        "clamped": False,
    }
    clamped = grok_concurrency_plan({"RESEARCH_LLM_CONCURRENCY": "40", "GROK_MAX_CONCURRENCY": "16"})
    assert clamped["clamped"] is True and clamped["effective"] == 16
    common = {
        "DATABASE_URL": "postgresql://unused/research_test",
        "DATA_DIR": "/tmp/research-grok-migration",
        "RESEARCH_LLM_CONCURRENCY": "99",
    }
    grok_settings = Settings.from_env({**common, "RESEARCH_LLM_PROVIDER": "grok"})
    deepseek_settings = Settings.from_env({**common, "RESEARCH_LLM_PROVIDER": "deepseek"})
    assert grok_settings.llm_concurrency == 99
    assert deepseek_settings.llm_concurrency == 99
    assert deepseek_settings.llm_provider == "deepseek"
    assert deepseek_settings.llm_base_url == "https://api.deepseek.com"


def test_retry_delays_are_configurable(monkeypatch):
    assert retry_delays({}) == RETRY_DELAYS_DEFAULT == (2.0, 5.0, 15.0, 30.0)
    assert retry_delays({"LLM_RETRY_DELAYS": "4,8,20"}) == (4.0, 8.0, 20.0)
    assert retry_delays({"LLM_RETRY_DELAYS": "4, 8"}) == (4.0, 8.0)
    # 填坏不让重试被悄悄关掉。
    assert retry_delays({"LLM_RETRY_DELAYS": "abc"}) == RETRY_DELAYS_DEFAULT
    assert retry_delays({"LLM_RETRY_DELAYS": "-1"}) == RETRY_DELAYS_DEFAULT
    monkeypatch.setenv("LLM_RETRY_DELAYS", "1,2")
    assert llm_module._delays() == (1.0, 2.0)


def test_on_text_streams_and_still_reports_usage(tmp_path, monkeypatch):
    """问库边写边显示：Grok 也走流式 SSE，正文增量交给 on_text，用量照样累加。"""
    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    seen_requests = []

    class _Response:
        def __init__(self):
            self._lines = [
                b'data: {"model": "grok-4.7", "choices": [{"delta": {"content": "\\u4e00"}}]}\n',
                b'data: {"model": "grok-4.7", "choices": [{"delta": {"content": "\\u4e8c"}}]}\n',
                b'data: {"model": "grok-4.7", "choices": [], "usage": {"prompt_tokens": 10, '
                b'"completion_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 3}}}\n',
                b"data: [DONE]\n",
            ]

        def readline(self):
            return self._lines.pop(0) if self._lines else b""

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout=None):
        seen_requests.append(json.loads(request.data))
        assert request.get_header("Accept") == "text/event-stream"
        return _Response()

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    usage: dict = {}
    shown: list[str] = []
    text, model = chat(
        _settings(tmp_path, XAI_OAUTH_TOKEN_PATH=str(token)),
        [{"role": "user", "content": "hi"}],
        usage=usage,
        on_text=shown.append,
    )
    payload = seen_requests[0]
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["reasoning_effort"] == "high" and payload["fast"] is False
    assert (text, model) == ("一二", "grok-4.7")
    assert shown[-1] == "一二"
    assert usage == {"prompt": 10, "completion": 4, "reasoning": 3}


def test_translate_strength_and_cache_hit_usage(tmp_path, monkeypatch):
    """译制和问库同一组强度；xAI 的缓存命中字段也算进用量。"""
    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    captured = {}

    class _Response:
        status = 200

        def read(self):
            return json.dumps(
                {
                    "model": "grok-4.7",
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 60},
                        "completion_tokens_details": {"reasoning_tokens": 12},
                    },
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _Response()

    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    with translate_llm.metered() as usage:
        result = translate_llm.chat_json(system="s", user={"q": "hi"}, purpose="t", subject="b", data_dir=tmp_path)
    body = captured["body"]
    assert body["model"] == "grok-4.7"
    assert body["reasoning_effort"] == "high" and body["fast"] is False
    assert "stream" not in body
    assert result.data == {"ok": True}
    assert (usage.prompt, usage.cache_hit, usage.completion, usage.reasoning) == (100, 60, 20, 12)


def _json_response(payload: dict):
    class _Response:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Response()


def test_chat_logs_429_then_retries(tmp_path, monkeypatch, caplog):
    """429 先记下来再退避。后来成功的那一次也算进 429 次数，不能只看最后有没有失败。"""
    import logging

    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    headers = EmailMessage()
    headers["Retry-After"] = "3"
    calls = {"n": 0}
    slept: list[float] = []

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm_module.urllib.error.HTTPError(
                request.full_url, 429, "slow", headers, io.BytesIO(b"slow down")
            )
        return _json_response({"choices": [{"message": {"content": "好"}}], "model": "grok-4.7"})

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_module.time, "sleep", slept.append)
    reset_http_stats()
    caplog.set_level(logging.WARNING, logger="researchlib.llm")
    text, model = chat(_settings(tmp_path, XAI_OAUTH_TOKEN_PATH=str(token)), [{"role": "user", "content": "hi"}])
    assert (text, model) == ("好", "grok-4.7")
    assert slept == [3.0]
    assert http_stats()["http_429"] == 1
    assert "rate-limit event=429" in caplog.text
    assert "provider=Grok" in caplog.text
    assert "caller=chat" in caplog.text
    assert "retry_after=1" in caplog.text
    assert "exhausted=0" in caplog.text


def test_translate_logs_429_until_the_budget_is_spent(tmp_path, monkeypatch, caplog):
    import logging

    from researchlib.translate.llm import LLMHTTPError

    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    monkeypatch.setenv("LLM_RETRY_DELAYS", "1,2")

    def fake_urlopen(request, timeout=None):
        raise translate_llm.urllib.error.HTTPError(
            request.full_url, 429, "slow", EmailMessage(), io.BytesIO(b"slow down")
        )

    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(translate_llm.time, "sleep", lambda _seconds: None)
    reset_http_stats()
    caplog.set_level(logging.WARNING, logger="researchlib.translate")
    with pytest.raises(LLMHTTPError) as caught:
        translate_llm.chat_json(system="s", user={"q": "hi"}, purpose="t", subject="b", data_dir=tmp_path)
    assert caught.value.status == 429
    assert http_stats()["http_429"] == 3
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("rate-limit")]
    assert len(lines) == 3
    assert lines[-1].endswith("exhausted=1") or "exhausted=1" in lines[-1]
    assert "caller=translate" in lines[0]


def test_smoke_refuses_when_provider_is_not_grok(monkeypatch):
    from researchlib import grok_smoke

    monkeypatch.setenv("RESEARCH_LLM_PROVIDER", "deepseek")
    assert grok_smoke.main([]) == 2

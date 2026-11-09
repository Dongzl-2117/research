"""2026-11-10 的迁移口径：站内 LLM 全走 Grok OAuth，不再默认 DeepSeek、失败也不回退。

这几条是这次迁移的硬要求，单独钉住：

- 默认提供方是 grok。宿主机 .env 里遗留的 ``RESEARCH_LLM_PROVIDER=deepseek`` 被忽略，
  只有显式的 ``deepseek-legacy`` 才走深求——免得部署时一个旧变量把站点悄悄留在 DeepSeek 上。
- 凭证不可用时直接报错，不去取 DeepSeek key，也不发第二个请求。
- 强度：grok-4.7 / reasoning_effort=high / fast=false / 50 万上下文 / 输出至少 128000。
- 并发：默认 8，硬顶 24，可用 RESEARCH_LLM_CONCURRENCY 调；退避间隔可用 LLM_RETRY_DELAYS 调。
- 透传 on_text（问库边写边显示）时走流式，用量仍要报回来。
"""

import json
import time

import pytest

from researchlib import grok, health
from researchlib import llm as llm_module
from researchlib.config import Settings
from researchlib.grok import (
    GROK_CONCURRENCY_CEILING,
    GROK_CONCURRENCY_DEFAULT,
    RETRY_DELAYS_DEFAULT,
    completion_params,
    grok_concurrency,
    retry_delays,
)
from researchlib.llm import LLMUnavailable, chat, credential_kind
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


def test_leftover_deepseek_env_does_not_keep_the_site_on_deepseek():
    """部署时 .env 里还留着 deepseek 也不会退回深求：只有 deepseek-legacy 才认。"""
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "deepseek"}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "deepseek-legacy"}) == "deepseek"
    assert llm_provider({}) == "grok"


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


def test_concurrency_defaults_and_ceiling(monkeypatch):
    assert grok_concurrency({}) == GROK_CONCURRENCY_DEFAULT == 8
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "16"}) == 16
    # 填得比硬顶大也只到 24，不因为环境变量把容器撑爆。
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "64"}) == GROK_CONCURRENCY_CEILING == 24
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "0"}) == 1
    assert grok_concurrency({"RESEARCH_LLM_CONCURRENCY": "abc"}) == 8
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused/research_test",
            "DATA_DIR": "/tmp/research-grok-migration",
            "RESEARCH_LLM_CONCURRENCY": "99",
        }
    )
    assert settings.llm_concurrency == GROK_CONCURRENCY_CEILING


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

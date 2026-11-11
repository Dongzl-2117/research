"""DeepSeek 分支（``RESEARCH_LLM_PROVIDER=deepseek``，别名 ``deepseek-legacy``）。

deepseek-flash / thinking / max / 输出至少 393216。

默认提供方是 Grok。这个文件里除默认值那两个测试外，其余都是按提供方测传输
（SSE 重拼、断线重试、超时不重发），所以 fixture 显式把提供方设成 deepseek-legacy。
"""

import json
import ssl
import time
import urllib.error

import pytest

from researchlib import deepseek, health
from researchlib import llm as llm_module
from researchlib.config import Settings
from researchlib.llm import (
    RETRY_DELAYS,
    IncompleteStream,
    LLMError,
    LLMUnavailable,
    active_label,
    active_model_id,
    chat,
    credential_kind,
    ensure_oauth,
    llm_configured,
    read_chat_completion,
)
from researchlib.llm_provider import llm_provider
from researchlib.translate import llm as translate_llm
from researchlib.translate.llm import LLMTransportError


class _Response:
    status = 200

    def __init__(self, payload: dict):
        self._buf = json.dumps(payload).encode()

    def read(self, _amt=-1):
        data, self._buf = self._buf, b""
        return data

    def readline(self, _limit=-1):
        if not self._buf:
            return b""
        cut = self._buf.find(b"\n")
        if cut < 0:
            data, self._buf = self._buf, b""
            return data
        line = self._buf[: cut + 1]
        self._buf = self._buf[cut + 1 :]
        return line

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    # 这个文件测 DeepSeek 那条分支的传输，显式设成应急回退值；默认提供方见下面两个测试。
    monkeypatch.setenv("RESEARCH_LLM_PROVIDER", "deepseek-legacy")
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
            # 这个文件按提供方测传输，显式走应急回退值；默认 Grok 的那组在 test_grok_strength.py。
            "RESEARCH_LLM_PROVIDER": "deepseek-legacy",
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


def test_provider_defaults_to_grok():
    """默认 Grok。deepseek 和别名 deepseek-legacy 都切到 DeepSeek；空值和拼错的值仍是 grok。"""
    assert llm_provider({}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": ""}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": " DeepSeek "}) == "deepseek"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "deepseek"}) == "deepseek"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "GROK"}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "xai"}) == "grok"
    assert llm_provider({"RESEARCH_LLM_PROVIDER": "deepseek-legacy"}) == "deepseek"


def test_settings_defaults_are_grok_oauth(tmp_path):
    # 不写提供方（_settings 里那个应急回退值要去掉），看真实默认。
    settings = Settings.from_env(
        {"DATABASE_URL": "postgresql://unused/research_test", "DATA_DIR": str(tmp_path)}
    )
    assert settings.llm_provider == "grok"
    assert settings.llm_base_url == "https://api.x.ai/v1"
    assert active_label(settings) == "Grok"
    assert active_model_id(settings) == "grok-4.7"
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
    assert body["stream"] is True
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


def test_deepseek_chat_retries_empty_content_with_bigger_budget(tmp_path, monkeypatch):
    """思考模式把 max_tokens 耗尽时，提高额度再要一次，不把 reasoning 当正文。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    payloads = []

    def fake_post(_settings, _token, payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "只想了很久"},
                        "finish_reason": "length",
                    }
                ],
                "model": "deepseek-flash",
            }
        return {
            "choices": [{"message": {"content": "正文来了", "reasoning_content": "想"}}],
            "model": "deepseek-flash",
        }

    monkeypatch.setattr(llm_module, "_post", fake_post)
    settings = _settings(tmp_path)
    text, model = chat(
        settings,
        [{"role": "user", "content": "hi"}],
        exact_max_tokens=512,
    )

    assert (text, model) == ("正文来了", "deepseek-flash")
    assert [item["max_tokens"] for item in payloads] == [512, 8704]
    assert len(payloads) == 2


def test_deepseek_chat_does_not_retry_without_reasoning(tmp_path, monkeypatch):
    """正文空但没思考内容，不是额度被吃光，不重发。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    calls = []

    def fake_post(_settings, _token, _payload):
        calls.append(1)
        return {"choices": [{"message": {"content": ""}}], "model": "deepseek-flash"}

    monkeypatch.setattr(llm_module, "_post", fake_post)
    with pytest.raises(LLMError, match="正文为空"):
        chat(_settings(tmp_path), [{"role": "user", "content": "hi"}], exact_max_tokens=512)
    assert len(calls) == 1


def test_empty_content_budget_bump_is_capped():
    assert llm_module._bumped_max_tokens(512) == 8704
    assert llm_module._bumped_max_tokens(100_000) == 131_072
    # 已经超过封顶值时不再发第二次同样的请求。
    assert llm_module._bumped_max_tokens(393_216) < 393_216


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
    assert body["stream"] is True
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
    assert payload["reasoning_effort"] == "high"
    assert payload["fast"] is False
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


def _ssl_eof() -> ssl.SSLError:
    return ssl.SSLError(
        "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1011)"
    )


class _Scripted:
    """按顺序交出 SSE 行；下一项若是异常，就在下一次读时抛出。"""

    def __init__(self, events):
        self._events = list(events)

    def readline(self, _limit=-1):
        if not self._events:
            return b""
        event = self._events.pop(0)
        if isinstance(event, BaseException):
            raise event
        if isinstance(event, str):
            if not event.endswith("\n"):
                event += "\n"
            event = event.encode()
        return event

    def read(self, _amt=-1):
        rest = []
        while self._events:
            event = self._events.pop(0)
            if isinstance(event, BaseException):
                raise event
            rest.append(event.encode() if isinstance(event, str) else event)
        return b"".join(rest)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _delta(content=None, reasoning=None, model=None, finish=None, usage=None) -> str:
    chunk: dict = {"choices": [{"delta": {}}]}
    delta = chunk["choices"][0]["delta"]
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if model:
        chunk["model"] = model
    if finish:
        chunk["choices"][0]["finish_reason"] = finish
    if usage:
        chunk["usage"] = usage
    return "data: " + json.dumps(chunk, ensure_ascii=False)


def _clean_sse(content: str) -> list:
    return [
        _delta(reasoning="先想一下", model="deepseek-flash"),
        _delta(content=content),
        _delta(finish="stop", usage={"total_tokens": 3}),
        "data: [DONE]",
    ]


def test_clean_sse_stream_assembles_content_not_reasoning():
    body = read_chat_completion(_Scripted(_clean_sse("完整译文")), stream=True)
    message = body["choices"][0]["message"]
    assert message["content"] == "完整译文"
    assert message["reasoning_content"] == "先想一下"
    assert body["model"] == "deepseek-flash"
    assert body["usage"] == {"total_tokens": 3}


def test_midstream_ssl_eof_discards_partial_bytes():
    partial = _Scripted([_delta(content="半截译文"), _ssl_eof()])
    with pytest.raises(IncompleteStream, match="UNEXPECTED_EOF_WHILE_READING") as caught:
        read_chat_completion(partial, stream=True)
    assert "半截" not in str(caught.value)


def test_truncated_sse_without_done_is_not_a_completion():
    """有 finish_reason、内容也能拼完，但没有 [DONE]，仍是半截流。"""
    truncated = _Scripted([_delta(content="半截译文"), _delta(finish="stop")])
    with pytest.raises(IncompleteStream, match=r"\[DONE\]") as caught:
        read_chat_completion(truncated, stream=True)
    assert "半截" not in str(caught.value)


def test_chat_retries_midstream_ssl_eof_and_returns_the_finished_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    settings = _settings(tmp_path)
    bodies = []
    outcomes = [
        _Scripted([_delta(content="半截译文"), _ssl_eof()]),
        _Scripted(_clean_sse("完整译文")),
    ]

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        assert timeout == settings.llm_timeout
        return outcomes.pop(0)

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)
    text, model = chat(settings, [{"role": "user", "content": "hi"}])

    assert (text, model) == ("完整译文", "deepseek-flash")
    assert "半截" not in text
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert bodies[0]["stream"] is True
    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "max"
    assert bodies[0]["max_tokens"] == deepseek.DEEPSEEK_OUTPUT_FLOOR


def test_chat_gives_up_after_the_retry_budget_without_partial_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        return _Scripted([_delta(content="半截译文"), _ssl_eof()])

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)
    with pytest.raises(LLMError, match="UNEXPECTED_EOF_WHILE_READING") as caught:
        chat(_settings(tmp_path), [{"role": "user", "content": "hi"}])
    assert len(calls) == len(RETRY_DELAYS) + 1
    assert "半截" not in str(caught.value)


def test_translate_retries_urlerror_and_midstream_eof_without_partial_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    bodies = []
    outcomes = [
        urllib.error.URLError(_ssl_eof()),
        _Scripted([_delta(content='{"t": ["半截"]}'), _ssl_eof()]),
        _Scripted(_clean_sse('{"t": ["全文"]}')),
    ]

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        assert timeout == 12
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    sleeps = []
    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(translate_llm.time, "sleep", sleeps.append)
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

    assert result.data == {"t": ["全文"]}
    assert result.model == "deepseek-flash"
    assert result.usage == {"total_tokens": 3}
    assert len(bodies) == 3
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["stream"] is True
    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "max"
    assert bodies[0]["max_tokens"] == deepseek.DEEPSEEK_OUTPUT_FLOOR
    assert "temperature" not in bodies[0]
    assert sleeps == list(RETRY_DELAYS[:2])


def test_translate_truncated_sse_is_not_returned_as_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        return _Scripted([_delta(content='{"t": ["半截"]}'), _delta(finish="stop")])

    sleeps = []
    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(translate_llm.time, "sleep", sleeps.append)
    with pytest.raises(LLMTransportError, match=r"\[DONE\]") as caught:
        translate_llm.chat_json(
            system="sys",
            user={"segments": ["hi"]},
            purpose="translate_body",
            subject="batch",
            data_dir=tmp_path,
            timeout_s=12,
        )
    assert len(calls) == len(RETRY_DELAYS) + 1
    assert sleeps == list(RETRY_DELAYS)
    assert calls[0] == calls[-1]
    assert "半截" not in str(caught.value)


def test_translate_timeout_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.URLError(TimeoutError("The read operation timed out"))

    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(translate_llm.time, "sleep", lambda _seconds: None)
    with pytest.raises(LLMTransportError, match="timed out"):
        translate_llm.chat_json(
            system="sys",
            user={"segments": ["hi"]},
            purpose="translate_body",
            subject="batch",
            data_dir=tmp_path,
            timeout_s=12,
        )
    assert calls == [1]


class _Clock:
    """译制量每次连了多久用的假钟；fake urlopen 每被调一次往前拨一下。"""

    def __init__(self, step: float):
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += self.step


def _translate_call(tmp_path):
    return translate_llm.chat_json(
        system="sys",
        user={"segments": ["hi"]},
        purpose="translate_body",
        subject="batch",
        data_dir=tmp_path,
        timeout_s=12,
    )


def test_translate_gives_up_saying_how_long_each_try_lasted(tmp_path, monkeypatch, caplog):
    """每次都连了 5 分钟才断是长请求被掐，几秒就断是网络不稳；报错和重发的日志里都写着，不用再猜。"""
    import logging

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    clock = _Clock(300)

    def fake_urlopen(req, timeout=None):
        clock.tick()
        return _Scripted([_delta(reasoning="想了很久"), _ssl_eof()])

    monkeypatch.setattr(translate_llm, "_clock", clock)
    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(translate_llm.time, "sleep", lambda _seconds: None)
    caplog.set_level(logging.INFO, logger="researchlib.translate")
    with pytest.raises(LLMTransportError) as caught:
        _translate_call(tmp_path)
    assert str(caught.value).startswith(
        "连不上 DeepSeek 接口（同一请求试了 5 次，各连了 300、300、300、300、300 秒）：SSE 中途断开："
    )
    assert "UNEXPECTED_EOF_WHILE_READING" in str(caught.value)
    retries = [record.getMessage() for record in caplog.records]
    assert len(retries) == len(RETRY_DELAYS)
    assert retries[0].startswith("译制请求连了 300 秒断开（SSE 中途断开：")
    assert retries[0].endswith("2 秒后第 2 次发同一请求")
    assert retries[-1].endswith("30 秒后第 5 次发同一请求")


def test_translate_timeout_says_it_waited_the_whole_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    clock = _Clock(12)

    def fake_urlopen(req, timeout=None):
        clock.tick()
        raise urllib.error.URLError(TimeoutError("The read operation timed out"))

    monkeypatch.setattr(translate_llm, "_clock", clock)
    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(translate_llm.time, "sleep", lambda _seconds: pytest.fail("超时不重发"))
    with pytest.raises(LLMTransportError) as caught:
        _translate_call(tmp_path)
    assert str(caught.value) == (
        "连不上 DeepSeek 接口（连了 12 秒，12 秒没收到数据算超时，不再重发）："
        "<urlopen error The read operation timed out>"
    )


def test_translate_http_error_after_retries_says_how_many_tries(tmp_path, monkeypatch):
    import io

    from researchlib.translate.llm import LLMHTTPError

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 503, "busy", None, io.BytesIO(b"server busy"))

    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(translate_llm.time, "sleep", lambda _seconds: None)
    with pytest.raises(LLMHTTPError) as caught:
        _translate_call(tmp_path)
    assert len(calls) == len(RETRY_DELAYS) + 1
    assert caught.value.status == 503
    assert str(caught.value) == "DeepSeek HTTP 503（同一请求试了 5 次）: server busy"


def test_only_failures_that_waiting_can_fix_count_as_transient():
    from researchlib.translate.llm import LLMConfigError, LLMHTTPError, transient_failure

    assert transient_failure(LLMTransportError("连不上 DeepSeek 接口：ssl eof"))
    assert transient_failure(LLMHTTPError(429, "DeepSeek HTTP 429"))
    assert transient_failure(LLMHTTPError(503, "DeepSeek HTTP 503"))
    for lasting in (
        LLMHTTPError(400, "DeepSeek HTTP 400: Content Exists Risk"),
        LLMHTTPError(401, "DeepSeek HTTP 401"),
        LLMHTTPError(402, "DeepSeek HTTP 402: Insufficient Balance"),
        LLMConfigError("没有配置 DEEPSEEK_API_KEY"),
        RuntimeError("DeepSeek 返回非 JSON"),
        None,
    ):
        assert not transient_failure(lasting)


def test_translate_usage_is_metered_per_thread_including_unusable_answers(tmp_path, monkeypatch):
    """research-translate 每篇的用量：同一线程里每次收到完整回答都记，回答用不了的也记；别的线程的不混进来。"""
    import threading

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    answers = {
        "good": {"choices": [{"message": {"content": '{"t": ["好"]}'}}], "model": "deepseek-flash",
                 "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 40, "completion_tokens": 30,
                           "completion_tokens_details": {"reasoning_tokens": 20}}},
        "broken": {"choices": [{"message": {"content": "不是 JSON"}}], "model": "deepseek-flash",
                   "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        "silent": {"choices": [{"message": {"content": '{"t": []}'}}], "model": "deepseek-flash"},
    }

    def fake_urlopen(req, timeout=None):
        return _Response(answers[json.loads(req.data.decode())["messages"][1]["content"]])

    monkeypatch.setattr(translate_llm.urllib.request, "urlopen", fake_urlopen)

    def ask(name):
        return translate_llm.chat_json(system="s", user=name, purpose="t", subject="b", data_dir=tmp_path)

    totals = {}

    def article(name, asks):
        with translate_llm.metered() as usage:
            for item in asks:
                try:
                    ask(item)
                except ValueError:
                    pass
        totals[name] = usage.as_dict()

    with translate_llm.metered() as outer:
        article("a", ["good", "broken", "silent"])
    other = threading.Thread(target=article, args=("b", ["good"]))
    other.start()
    other.join()
    assert totals["a"] == {"requests": 3, "prompt": 107, "cacheHit": 40, "completion": 33, "reasoning": 20}
    # 没报用量的那次（silent）照算请求，说出来，不当成零花费。
    assert outer.unreported == 1 and outer.describe().endswith("其中 1 次接口没报用量、没算进去")
    # 套着用时外面那份也记；别的线程那篇只记在它自己那份里。
    assert outer.as_dict() == totals["a"]
    assert totals["b"] == {"requests": 1, "prompt": 100, "cacheHit": 40, "completion": 30, "reasoning": 20}
    # 不在记账时照常调用，不记。
    assert ask("good").usage["prompt_tokens"] == 100


def test_on_text_sees_the_answer_grow_and_is_reset_before_a_retry(tmp_path, monkeypatch):
    """问库边写边显示：正文每多一截交一次；读到一半断了，重发前先交空串，把半截收回。思考过程不交。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    outcomes = [
        _Scripted([_delta(reasoning="想一下"), _delta(content="半截"), _ssl_eof()]),
        _Scripted(
            [
                _delta(reasoning="想一下", model="deepseek-flash"),
                _delta(content="完整"),
                _delta(content="回答"),
                _delta(finish="stop"),
                "data: [DONE]",
            ]
        ),
    ]
    monkeypatch.setattr(llm_module.urllib.request, "urlopen", lambda req, timeout=None: outcomes.pop(0))
    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)
    seen: list[str] = []
    text, model = chat(_settings(tmp_path), [{"role": "user", "content": "hi"}], on_text=seen.append)
    assert (text, model) == ("完整回答", "deepseek-flash")
    assert seen == ["半截", "", "完整", "完整回答"]


def test_on_text_failures_do_not_break_the_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(
        llm_module.urllib.request, "urlopen", lambda req, timeout=None: _Scripted(_clean_sse("回答"))
    )

    def broken(_text):
        raise RuntimeError("写库失败")

    text, _model = chat(_settings(tmp_path), [{"role": "user", "content": "hi"}], on_text=broken)
    assert text == "回答"


def test_chat_holds_the_callers_slot_instead_of_the_shared_limit(tmp_path, monkeypatch):
    """问库分批读资料用自己的并发上限：占着调用方给的那份，不占摘要、观点共用的那份。"""
    import threading

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    settings = _settings(tmp_path)
    own = threading.BoundedSemaphore(1)
    shared = llm_module._slot(settings)
    held = []

    def fake_urlopen(req, timeout=None):
        held.append(not own.acquire(blocking=False))
        free = shared.acquire(blocking=False)
        if free:
            shared.release()
        held.append(free)
        return _Scripted(_clean_sse("摘录"))

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    text, _model = chat(settings, [{"role": "user", "content": "hi"}], slot=own)
    assert text == "摘录" and held == [True, True]
    assert own.acquire(blocking=False)
    own.release()


@pytest.mark.parametrize(
    ("utc", "until"),
    [
        # 2026-11-11 是周三。UTC 1 到 4 点、6 到 10 点（北京时间 9 到 12 点、14 到 18 点）是高峰。
        ((2026, 11, 11, 0, 59), None),
        ((2026, 11, 11, 1, 0), (2026, 11, 11, 4, 0)),
        ((2026, 11, 11, 3, 59), (2026, 11, 11, 4, 0)),
        ((2026, 11, 11, 4, 0), None),
        ((2026, 11, 11, 5, 30), None),
        ((2026, 11, 11, 6, 0), (2026, 11, 11, 10, 0)),
        ((2026, 11, 11, 9, 59), (2026, 11, 11, 10, 0)),
        ((2026, 11, 11, 10, 0), None),
        ((2026, 11, 13, 7, 0), (2026, 11, 13, 10, 0)),
        # 周六、周日全天不是高峰。
        ((2026, 11, 14, 2, 0), None),
        ((2026, 11, 15, 7, 0), None),
    ],
)
def test_deepseek_peak_hours_follow_the_price_list(utc, until):
    from datetime import UTC, datetime, timedelta, timezone

    now = datetime(*utc, tzinfo=UTC)
    expected = datetime(*until, tzinfo=UTC) if until else None
    assert deepseek.deepseek_peak_until(now) == expected
    # 换成北京时间传进来也一样。
    beijing = now.astimezone(timezone(timedelta(hours=8)))
    assert deepseek.deepseek_peak_until(beijing) == expected


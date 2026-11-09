"""站内调 LLM 做译制：默认 DeepSeek，Grok 作为显式选项。

默认 DeepSeek：deepseek-flash + thinking + reasoning_effort=max + 输出至少 393216，
上下文 1M，不发 temperature，鉴权用 DEEPSEEK_API_KEY 的 Bearer。请求走流式，
按行读到 ``data: [DONE]`` 才采用；中途 TLS EOF 按与问库相同的间隔重试同一请求，
半截内容不当译文。重试完还连不上时，报错里写明同一请求试了几次、每次连了几秒，
分得清网络一直不稳（每次几秒就断）和长请求被掐（每次都连了差不多一样久才断）。
``RESEARCH_LLM_PROVIDER=grok`` 时走 xAI OAuth（grok-4.7 + xhigh + 至少 128000，不开 fast），不流式。
模型、推理强度、输出地板和超时与问库、摘要共用 researchlib.deepseek / researchlib.grok。
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from researchlib.deepseek import (
    DEEPSEEK_CONTEXT_TOKENS,
    DEEPSEEK_MODEL_ID,
    DEEPSEEK_OUTPUT_FLOOR,
    DEEPSEEK_REASONING_EFFORT,
    deepseek_output_tokens,
)
from researchlib.deepseek import completion_params as deepseek_params
from researchlib.grok import (
    GROK_CONTEXT_TOKENS,
    GROK_INPUT_CHARS,
    GROK_MODEL_ID,
    GROK_OUTPUT_FLOOR,
    GROK_REASONING_EFFORT,
    grok_output_tokens,
    long_timeout_s,
)
from researchlib.grok import completion_params as grok_params
from researchlib.llm_provider import (
    PROVIDER_GROK,
    deepseek_api_key,
    llm_base_url,
    llm_provider,
    provider_label,
)
from researchlib.translate.xai_oauth import (
    XaiLoginRequiredError,
    get_xai_access_token,
    is_stale_xai_access,
)

logger = logging.getLogger("researchlib.translate")


class LLMTransportError(RuntimeError):
    """同一请求的连接错误已经按 RETRY_DELAYS 重试完。半截响应没有当成译文。"""


class LLMHTTPError(RuntimeError):
    """接口回了 HTTP 错误（429、5xx 已经按 RETRY_DELAYS 重试完）。

    ``status`` 给译制分辨：400 是这一次请求本身不行（内容被拒、太长），换小一点的批可能就过；
    401、402、429、5xx 是账号或服务的事，换哪一段问都一样。
    """

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class LLMConfigError(RuntimeError):
    """没配密钥这类本机配置问题，问哪一段都一样。"""


def transient_failure(error: BaseException | None) -> bool:
    """断线、超时、限流、5xx：服务那头一时的事，隔几分钟再问同一篇多半就成了。

    欠费（402）、鉴权（401、403）、没配密钥、内容被拒（400）隔多久再问都一样，不算。
    """
    if isinstance(error, LLMTransportError):
        return True
    return isinstance(error, LLMHTTPError) and (error.status == 429 or error.status >= 500)


# 量每一次请求连了多久。测试换成假钟。
_clock = time.monotonic


def _tries(spans: list[float], *, timed_out: bool, limit: float) -> str:
    """同一请求试了几次、每次连了几秒才失败，写在连不上的报错前头（报错会被截短，放后面容易截掉）。"""
    seconds = "、".join(f"{span:.0f}" for span in spans)
    note = f"同一请求试了 {len(spans)} 次，各连了 {seconds} 秒" if len(spans) > 1 else f"连了 {seconds} 秒"
    if timed_out:
        note += f"，{limit:.0f} 秒没收到数据算超时，不再重发"
    return note


@dataclass
class ChatJsonResult:
    data: dict[str, Any]
    model: str
    usage: dict[str, Any] | None
    latency_ms: int


@dataclass
class Usage:
    """模型用量，按 DeepSeek 返回的 usage 累加。接口没报用量的那一次只计请求数。"""

    prompt: int = 0
    cache_hit: int = 0
    completion: int = 0
    reasoning: int = 0
    requests: int = 0
    # 收到了回答、但接口没报用量的次数。不为 0 时上面几个数偏少。
    unreported: int = 0

    def add(self, usage: dict | None) -> None:
        self.requests += 1
        if not isinstance(usage, dict):
            self.unreported += 1
            return
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.cache_hit += int(usage.get("prompt_cache_hit_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            self.reasoning += int(details.get("reasoning_tokens") or 0)

    def merge(self, other: Usage) -> None:
        self.prompt += other.prompt
        self.cache_hit += other.cache_hit
        self.completion += other.completion
        self.reasoning += other.reasoning
        self.requests += other.requests
        self.unreported += other.unreported

    def as_dict(self) -> dict[str, int]:
        return {"requests": self.requests, "prompt": self.prompt, "cacheHit": self.cache_hit,
                "completion": self.completion, "reasoning": self.reasoning}

    def describe(self) -> str:
        return (
            f"请求 {self.requests} 次，输入 {self.prompt:,} token（缓存命中 {self.cache_hit:,}），"
            f"输出 {self.completion:,} token（思考 {self.reasoning:,}）"
            + (f"，其中 {self.unreported} 次接口没报用量、没算进去" if self.unreported else "")
        )


# 正在记账的用量。chat_json 每收到一次完整的回答（回答本身用不用得上都算）就往这里每一份各记一笔。
# 各线程各记各的：几篇同时译时，每篇只记自己那几次请求。
_meters: ContextVar[tuple[Usage, ...]] = ContextVar("translate_usage", default=())


@contextmanager
def metered() -> Iterator[Usage]:
    """这一段里（同一线程）chat_json 的用量记进交出去的 Usage。可以套着用，外面那份也照记。

    断在半路、没收到完整回答的那一次不记：接口没报用量，也不知道算没算钱。
    """
    usage = Usage()
    token = _meters.set((*_meters.get(), usage))
    try:
        yield usage
    finally:
        _meters.reset(token)


def output_tokens(requested: int | None = None) -> int:
    """当前提供方的输出预算。DeepSeek 地板 393216，Grok 128000。"""
    if llm_provider() == PROVIDER_GROK:
        return grok_output_tokens(requested)
    return deepseek_output_tokens(requested)


def model_id() -> str:
    """当前提供方的模型名。"""
    if llm_provider() == PROVIDER_GROK:
        return GROK_MODEL_ID
    return DEEPSEEK_MODEL_ID


def _params(temperature: float, max_tokens: int | None) -> dict:
    if llm_provider() == PROVIDER_GROK:
        return grok_params(temperature=temperature, max_tokens=max_tokens)
    # 思考模式下 temperature 不起作用，DeepSeek 的请求里不带。
    return deepseek_params(max_tokens=max_tokens)


def _bearer(data_dir: Path | None) -> str:
    if llm_provider() == PROVIDER_GROK:
        return get_xai_access_token(data_dir)
    key = deepseek_api_key()
    if not key:
        raise LLMConfigError(
            "没有配置 DEEPSEEK_API_KEY，DeepSeek 译不了。"
            "把 key 写进 .env（compose 的 DEEPSEEK_API_KEY），或者设 RESEARCH_LLM_PROVIDER=grok 用回 Grok OAuth。"
        )
    return key


def extract_json(text: str) -> Any:
    """从模型输出里取 JSON：整段、代码围栏，或首尾花括号之间。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        return json.loads(fence.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"unusable JSON: {text[:200]}")


def chat_json(
    *,
    system: str,
    user: Any,
    purpose: str,
    subject: str,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    data_dir: Path | None = None,
    timeout_s: float | None = None,
) -> ChatJsonResult:
    """POST {base}/chat/completions。强度按当前提供方，DeepSeek 默认 max / 393216 / thinking，并走流式。"""
    # llm 加载时会进 translate 包。顶上再导 researchlib.llm 会环，所以放在调用里。
    from researchlib.llm import RETRY_DELAYS, RETRY_STATUS, IncompleteStream, read_chat_completion
    from researchlib.llm import _is_timeout as _transport_timeout
    from researchlib.llm import _retry_after as _http_retry_after

    del purpose, subject  # 保留形参便于日后打回执；当前不落库
    provider = llm_provider()
    label = provider_label(provider)
    endpoint = f"{llm_base_url()}/chat/completions"
    user_text = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
    params = _params(temperature, max_tokens)
    # 思考模式若不流式，响应头要等整段推理结束才到，连接中间没有字节，TLS 会被对端拆掉。
    # 流式让 reasoning 增量保持连接有数据。模型、thinking、reasoning_effort、max_tokens 不改。
    stream = provider != PROVIDER_GROK and str(params["model"]).startswith("deepseek")
    body = {
        **params,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_object"},
    }
    if stream:
        body["stream"] = True
    payload = json.dumps(body).encode("utf-8")
    timeout = timeout_s if timeout_s is not None else long_timeout_s()
    bearer = _bearer(data_dir)

    def post(token: str) -> dict:
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "accept": "text/event-stream" if stream else "application/json",
            },
        )
        # timeout 同时是连接和每一次读的空闲上限，思考模式默认 25 分钟。
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return read_chat_completion(resp, stream=stream)

    started = time.time()
    refreshed = False
    retries = 0
    # 失败的每一次从发出到出错连了几秒。几秒就断是网络不稳；每次都连了差不多一样久才断，是长请求被掐。
    spans: list[float] = []
    while True:
        begun = _clock()
        try:
            response = post(bearer)
            break
        except urllib.error.HTTPError as exc:
            spans.append(_clock() - begun)
            detail = exc.read().decode("utf-8", "replace")
            if provider == PROVIDER_GROK and not refreshed and is_stale_xai_access(exc.code, detail):
                refreshed = True
                bearer = get_xai_access_token(data_dir, force=True, discard_access_token=bearer)
                continue
            if exc.code in RETRY_STATUS and retries < len(RETRY_DELAYS):
                delay = _http_retry_after(exc) or RETRY_DELAYS[retries]
                logger.info("译制请求 HTTP %s，%.0f 秒后第 %s 次发同一请求", exc.code, delay, len(spans) + 1)
                time.sleep(delay)
                retries += 1
                continue
            tried = f"（同一请求试了 {len(spans)} 次）" if len(spans) > 1 else ""
            raise LLMHTTPError(exc.code, f"{label} HTTP {exc.code}{tried}: {detail[:500]}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label} 返回非 JSON") from exc
        except (urllib.error.URLError, IncompleteStream, OSError, http.client.HTTPException) as exc:
            spans.append(_clock() - begun)
            # 超时表示这一次已经等满了长超时，不再重发。其余连接错误重试同一份 payload。
            timed_out = _transport_timeout(exc)
            if timed_out or retries >= len(RETRY_DELAYS):
                note = _tries(spans, timed_out=timed_out, limit=timeout)
                raise LLMTransportError(f"连不上 {label} 接口（{note}）：{exc}") from exc
            delay = RETRY_DELAYS[retries]
            logger.info(
                "译制请求连了 %.0f 秒断开（%s），%.0f 秒后第 %s 次发同一请求", spans[-1], exc, delay, len(spans) + 1
            )
            time.sleep(delay)
            retries += 1
    reported = response.get("usage") if isinstance(response.get("usage"), dict) else None
    for meter in _meters.get():
        meter.add(reported)
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    # 思考过程在 reasoning_content，只取最终答案。
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    data = extract_json(content)
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} JSON 根节点不是对象")
    return ChatJsonResult(
        data=data,
        model=str(response.get("model") or params["model"]),
        usage=reported,
        latency_ms=int((time.time() - started) * 1000),
    )


__all__ = [
    "DEEPSEEK_CONTEXT_TOKENS",
    "DEEPSEEK_MODEL_ID",
    "DEEPSEEK_OUTPUT_FLOOR",
    "DEEPSEEK_REASONING_EFFORT",
    "GROK_CONTEXT_TOKENS",
    "GROK_INPUT_CHARS",
    "GROK_MODEL_ID",
    "GROK_OUTPUT_FLOOR",
    "GROK_REASONING_EFFORT",
    "ChatJsonResult",
    "LLMConfigError",
    "LLMHTTPError",
    "LLMTransportError",
    "Usage",
    "XaiLoginRequiredError",
    "chat_json",
    "extract_json",
    "grok_output_tokens",
    "long_timeout_s",
    "metered",
    "model_id",
    "output_tokens",
    "transient_failure",
]

"""调用站内 LLM（OpenAI 兼容的 chat/completions）。

提供方由 ``RESEARCH_LLM_PROVIDER`` 决定：默认 DeepSeek，用 ``DEEPSEEK_API_KEY`` 加 Bearer；
``RESEARCH_LLM_PROVIDER=grok`` 时走与 AIHOT 共用的 xAI OAuth
（``XAI_OAUTH_TOKEN_PATH``，Docker 默认 ``/aihot-data/secrets/xai-oauth.json``）。
DeepSeek 缺 key 时直接报错，不会悄悄退回 Grok；Grok 路径照旧不读 ``XAI_API_KEY``。

强度与译制共用：DeepSeek 见 ``researchlib.deepseek``（deepseek-flash、thinking 打开、
reasoning_effort=max、输出不低于 393216、上下文 1M、不发 temperature），
Grok 见 ``researchlib.grok``（grok-4.7、reasoning_effort=xhigh、输出不低于 128000、50 万上下文、不发 fast）。

只用标准库发请求，HTTPS_PROXY 由 urllib 自己读取。
429、5xx 和连不上这类临时错误退避重试两次；超时不重试（一次已经等了很久）。同一个进程里同时在跑的
请求有上限（RESEARCH_LLM_CONCURRENCY，默认 3），问库分批、摘要、观点、论点共用。
每次调用的最终结果记进 DATA_DIR/status/llm.json，连续失败或凭证不可用时 research-status 会列出来。
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request

from researchlib.config import Settings
from researchlib.deepseek import DEEPSEEK_MODEL_ID, deepseek_model
from researchlib.deepseek import completion_params as deepseek_params
from researchlib.grok import GROK_MODEL_ID
from researchlib.grok import completion_params as grok_params
from researchlib.health import record_llm
from researchlib.llm_provider import (
    PROVIDER_GROK,
    deepseek_api_key,
    llm_provider,
    provider_label,
)
from researchlib.translate.xai_oauth import (
    XaiLoginRequiredError,
    get_xai_access_token,
    is_stale_xai_access,
    xai_oauth_status,
)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# 第一次重试前等 5 秒，第二次等 20 秒。接口给了 Retry-After 就按它来，最多等一分钟。
RETRY_DELAYS = (2.0, 5.0, 15.0, 30.0)
RETRY_AFTER_CAP = 60.0

_slot_lock = threading.Lock()
_slots: dict[int, threading.BoundedSemaphore] = {}


class LLMUnavailable(Exception):
    """没有可用的 LLM 凭证。页面退化为只给检索结果。"""


class LLMError(Exception):
    """接口返回错误或格式不对。"""


def _provider(settings: Settings) -> str:
    return getattr(settings, "llm_provider", "") or llm_provider()


def active_label(settings: Settings) -> str:
    """"DeepSeek" 或 "Grok"。首页和报错用。"""
    return provider_label(_provider(settings))


def active_model_id(settings: Settings) -> str:
    """当前提供方的模型名。页面展示用，配错时不让整页报错。"""
    if _provider(settings) == PROVIDER_GROK:
        return GROK_MODEL_ID
    try:
        return deepseek_model()
    except RuntimeError:
        return DEEPSEEK_MODEL_ID


def credential_kind(settings: Settings) -> str:
    """"deepseek"、"oauth"，或 ""（当前提供方没有凭证）。"""
    if _provider(settings) == PROVIDER_GROK:
        return "oauth" if xai_oauth_status(settings.data_dir).logged_in else ""
    return "deepseek" if deepseek_api_key() else ""


def llm_configured(settings: Settings) -> bool:
    return credential_kind(settings) in {"deepseek", "oauth"}


def _oauth_token(settings: Settings, *, force: bool = False, discard: str | None = None) -> str:
    try:
        return get_xai_access_token(
            settings.data_dir,
            force=force,
            discard_access_token=discard,
        )
    except XaiLoginRequiredError as exc:
        extra = " 环境变量 XAI_API_KEY 不会被使用。" if os.environ.get("XAI_API_KEY", "").strip() else ""
        raise LLMUnavailable(f"{exc}{extra}") from exc


def _deepseek_token() -> str:
    key = deepseek_api_key()
    if not key:
        raise LLMUnavailable(
            "没有配置 DEEPSEEK_API_KEY，DeepSeek 调不了。"
            "把 key 写进 .env（docker compose 的 DEEPSEEK_API_KEY），"
            "或者设 RESEARCH_LLM_PROVIDER=grok 用回 Grok OAuth。"
        )
    return key


def ensure_llm(settings: Settings) -> None:
    """命令行入口在开工前调用。凭证不对就抛 LLMUnavailable，不换提供方硬撑。"""
    if _provider(settings) == PROVIDER_GROK:
        _oauth_token(settings)
    else:
        _deepseek_token()


def ensure_oauth(settings: Settings) -> None:
    """旧调用名。现在两家都管，实际走 ensure_llm。"""
    ensure_llm(settings)


def _slot(settings: Settings) -> threading.BoundedSemaphore:
    limit = max(1, int(getattr(settings, "llm_concurrency", 3) or 3))
    with _slot_lock:
        found = _slots.get(limit)
        if found is None:
            found = _slots[limit] = threading.BoundedSemaphore(limit)
    return found


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = (exc.headers or {}).get("Retry-After") if exc.headers is not None else None
    try:
        return min(RETRY_AFTER_CAP, max(0.0, float(raw))) if raw else None
    except (TypeError, ValueError):
        return None


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def _failed(settings: Settings, exc: Exception, *, provider: str | None = None) -> Exception:
    record_llm(
        getattr(settings, "data_dir", None),
        ok=False,
        error=str(exc),
        unavailable=isinstance(exc, LLMUnavailable),
        provider=provider or active_label(settings),
    )
    return exc


def _post(settings: Settings, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        settings.llm_base_url + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    stream = str(payload.get("model", "")).startswith("deepseek")
    if stream:
        # 思考模式长时间不出字，代理会断开空闲连接；流式让连接一直有数据。
        request.data = json.dumps({**payload, "stream": True}, ensure_ascii=False).encode("utf-8")
        request.add_header("Accept", "text/event-stream")
    with urllib.request.urlopen(request, timeout=settings.llm_timeout) as response:
        text = response.read().decode("utf-8", "replace")
    if not stream or text.lstrip().startswith("{"):
        return json.loads(text)
    return _read_stream(text.splitlines())


def _read_stream(lines) -> dict:
    """把 SSE 分片拼回非流式的 choices[0].message 结构。"""
    content: list[str] = []
    reasoning: list[str] = []
    model = ""
    usage = None
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        model = chunk.get("model") or model
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
    body = {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning)}}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _message_content(body: dict, label: str) -> str:
    """取出 choices[0].message 的最终正文。思考过程在 reasoning_content，不算回答。

    content 可能是字符串，也可能是带 text 字段的分段列表；缺字段或洗完是空串都算失败，
    避免问库把空回答标成 done。
    """
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{label} 接口返回里没有 choices[0].message") from exc
    if not isinstance(message, dict):
        raise LLMError(f"{label} 接口返回里没有 choices[0].message")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    if content is None:
        content = ""
    elif not isinstance(content, str):
        content = str(content)
    text = content.strip()
    if not text:
        raise LLMError(f"{label} 接口返回的正文为空")
    return text


def chat(
    settings: Settings,
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    exact_max_tokens: int | None = None,
) -> tuple[str, str]:
    """返回 (回答文本, 实际模型名)。

    DeepSeek：deepseek-flash / thinking / reasoning_effort=max / 至少 393216，不发 temperature。
    Grok：grok-4.7 / xhigh / 至少 128000，不发 fast。
    ``exact_max_tokens`` 会覆盖上面的下限（识图转写不需要几十万输出，否则容器 OOM）。
    """
    provider = _provider(settings)
    label = provider_label(provider)
    if provider == PROVIDER_GROK:
        payload = {**grok_params(temperature=temperature, max_tokens=max_tokens), "messages": messages}
    else:
        # 思考模式下 temperature 不起作用，DeepSeek 的请求里不带。
        payload = {**deepseek_params(max_tokens=max_tokens), "messages": messages}
    if exact_max_tokens is not None:
        payload["max_tokens"] = int(exact_max_tokens)
    try:
        token = _oauth_token(settings) if provider == PROVIDER_GROK else _deepseek_token()
    except LLMUnavailable as exc:
        raise _failed(settings, exc, provider=label) from None
    body = None
    refreshed = False
    retries = 0
    while True:
        try:
            with _slot(settings):
                body = _post(settings, token, payload)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if provider == PROVIDER_GROK and not refreshed and is_stale_xai_access(exc.code, detail):
                # 过期的 access token 被报成 401/403：强制刷新一次再试，不算在重试次数里。
                refreshed = True
                try:
                    token = _oauth_token(settings, force=True, discard=token)
                except LLMUnavailable as unavailable:
                    raise _failed(settings, unavailable, provider=label) from None
                continue
            if exc.code in RETRY_STATUS and retries < len(RETRY_DELAYS):
                time.sleep(_retry_after(exc) or RETRY_DELAYS[retries])
                retries += 1
                continue
            raise _failed(settings, LLMError(f"{label} 接口返回 {exc.code}：{detail}"), provider=label) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as exc:
            if not _is_timeout(exc) and retries < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[retries])
                retries += 1
                continue
            raise _failed(settings, LLMError(f"连不上 {label} 接口：{exc}"), provider=label) from exc
        except json.JSONDecodeError as exc:
            raise _failed(settings, LLMError(f"{label} 接口返回的不是 JSON"), provider=label) from exc
    try:
        text = _message_content(body, label)
    except LLMError as exc:
        raise _failed(settings, exc, provider=label) from None
    record_llm(settings.data_dir, ok=True, provider=label)
    return text, str(body.get("model") or payload["model"])

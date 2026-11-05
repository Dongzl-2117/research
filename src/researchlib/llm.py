"""调用站内 LLM（OpenAI 兼容的 chat/completions）。

提供方由 ``RESEARCH_LLM_PROVIDER`` 决定：默认 DeepSeek，用 ``DEEPSEEK_API_KEY`` 加 Bearer；
``RESEARCH_LLM_PROVIDER=grok`` 时走 xAI OAuth
（``XAI_OAUTH_TOKEN_PATH``，compose 里是 ``/data/secrets/xai-oauth.json``，用 ``research-xai-login login`` 登录）。
DeepSeek 缺 key 时直接报错，不会悄悄退回 Grok；Grok 路径照旧不读 ``XAI_API_KEY``。

强度与译制共用：DeepSeek 见 ``researchlib.deepseek``（deepseek-flash、thinking 打开、
reasoning_effort=max、输出不低于 393216、上下文 1M、不发 temperature），
Grok 见 ``researchlib.grok``（grok-4.7、reasoning_effort=xhigh、输出不低于 128000、50 万上下文、不发 fast）。

只用标准库发请求，HTTPS_PROXY 由 urllib 自己读取。
429、5xx 和连不上（含流读到一半的 TLS EOF）按 RETRY_DELAYS 退避重试；超时不重试。
DeepSeek 打开流式，按行读 SSE。没看到 ``data: [DONE]`` 就丢掉已经拼上的半截，不当作成功。
同一个进程里同时在跑的请求有上限（RESEARCH_LLM_CONCURRENCY，默认 3），问库分批、摘要、观点、论点共用。
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
# 依次等 2、5、15、30 秒。接口给了 Retry-After 就按它来，最多等一分钟。
# 译制的 chat_json 用同一组间隔，改这里两边一起变。
RETRY_DELAYS = (2.0, 5.0, 15.0, 30.0)
RETRY_AFTER_CAP = 60.0
# 思考模式把 max_tokens 花在 reasoning_content 上时，正文会是空的。
# 这时把额度翻倍再要一次；封顶 131072，识图这类长输出也不至于把容器撑爆。
EMPTY_CONTENT_MAX_TOKENS = 131_072

_slot_lock = threading.Lock()
_slots: dict[int, threading.BoundedSemaphore] = {}


class LLMUnavailable(Exception):
    """没有可用的 LLM 凭证。页面退化为只给检索结果。"""


class LLMError(Exception):
    """接口返回错误或格式不对。"""


class IncompleteStream(OSError):
    """SSE 在 ``data: [DONE]`` 之前断了。已经读到的半截不能当成一次成功的回答。"""


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


def _decode_chunk(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")


def _read_body(response) -> str:
    return _decode_chunk(response.read())


def _iter_text_lines(response):
    """按行读响应。每一行最多等 urlopen 传入的超时，思考模式用的是长超时。"""
    while True:
        raw = response.readline()
        if not raw:
            return
        yield _decode_chunk(raw)


def _sse_done(line: str) -> bool:
    text = line.strip()
    if not text.startswith("data:"):
        return False
    return text[5:].strip() == "[DONE]"


def read_chat_completion(response, *, stream: bool) -> dict:
    """读完一次 chat/completions。

    流式时逐行读，看到 ``data: [DONE]`` 才把增量拼回 message。
    中途 SSL、连接重置或流提前结束都抛 IncompleteStream，调用方重试；已经读到的字不返回。
    对端若忽略 stream、回了一整段 JSON，仍按非流式解析。
    每一行的等待上限是 urlopen 的 timeout（思考模式默认 25 分钟）。
    不另设更短的空闲超时，避免把还在思考的连接当成断开再花一次。
    """
    if not stream:
        return json.loads(_read_body(response))
    lines: list[str] = []
    try:
        for line in _iter_text_lines(response):
            if not lines and not line.strip():
                continue
            if not lines and line.lstrip().startswith("{"):
                return json.loads(line + _read_body(response))
            lines.append(line)
            if _sse_done(line):
                return _read_stream(lines)
    except IncompleteStream:
        raise
    except TimeoutError:
        raise
    except json.JSONDecodeError:
        raise
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise IncompleteStream(f"SSE 中途断开：{exc}") from exc
    raise IncompleteStream("SSE 在 [DONE] 之前结束")


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
        # 思考模式长时间不出字，中间设备会拆掉空闲连接。流式让推理增量一直往连接上写。
        request.data = json.dumps({**payload, "stream": True}, ensure_ascii=False).encode("utf-8")
        request.add_header("Accept", "text/event-stream")
    # 套接字超时是 settings.llm_timeout：连接和随后每一次读都用它，默认 25 分钟。
    with urllib.request.urlopen(request, timeout=settings.llm_timeout) as response:
        return read_chat_completion(response, stream=stream)


def _read_stream(lines) -> dict:
    """把 SSE 分片拼回非流式的 choices[0].message 结构。"""
    content: list[str] = []
    reasoning: list[str] = []
    model = ""
    usage = None
    done = False
    finished = False
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        if not data:
            continue
        try:
            chunk = json.loads(data)
        except ValueError as exc:
            raise LLMError('模型流包含无效 JSON') from exc
        if not isinstance(chunk, dict) or chunk.get('error'):
            raise LLMError('模型流返回错误')
        model = chunk.get("model") or model
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                raise LLMError('模型流包含无效候选')
            if choice.get('index', 0) != 0:
                continue
            reason = choice.get('finish_reason')
            if reason is not None:
                if reason != 'stop':
                    raise LLMError('模型流未完整完成')
                finished = True
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
    if not done or not finished:
        raise LLMError('模型流中途结束，缺少完成标记')
    body = {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning)},
             "finish_reason": 'stop'}
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


def _reasoning_text(body: dict) -> str:
    """取 message.reasoning_content（思考过程），没有就返回空串。"""
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    if not isinstance(message, dict):
        return ""
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, list):
        reasoning = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in reasoning
        )
    return reasoning.strip() if isinstance(reasoning, str) else ""


def _bumped_max_tokens(current: int) -> int:
    """正文被思考挤空时的备用额度：取两倍与加 8192 的较大者，封顶 131072。"""
    return min(max(current * 2, current + 8192), EMPTY_CONTENT_MAX_TOKENS)


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
    budget_bumped = False
    while True:
        try:
            with _slot(settings):
                body = _post(settings, token, payload)
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
            current = int(payload.get("max_tokens") or 0)
            bumped = _bumped_max_tokens(current)
            if not budget_bumped and bumped > current and _reasoning_text(body):
                # 思考模式把额度耗尽时 content 为空、reasoning_content 有字：
                # 提高 max_tokens 再要一次（只加这一次，不占 HTTP 重试次数）。
                payload["max_tokens"] = bumped
                budget_bumped = True
                continue
            raise _failed(settings, exc, provider=label) from None
        break
    record_llm(settings.data_dir, ok=True, provider=label)
    return text, str(body.get("model") or payload["model"])

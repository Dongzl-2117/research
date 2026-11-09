"""调用站内 LLM（OpenAI 兼容的 chat/completions）。

提供方由 ``RESEARCH_LLM_PROVIDER`` 切换，默认 Grok OAuth（``grok-4.7``，``reasoning_effort=high``，
``fast=false``，50 万上下文），强度见 ``researchlib.grok``。鉴权是 xAI OAuth
（``XAI_OAUTH_TOKEN_PATH``，compose 里是 ``/data/secrets/xai-oauth.json``，用 ``research-xai-login login`` 登录）。
``RESEARCH_LLM_PROVIDER=deepseek`` 走 DeepSeek（``DEEPSEEK_API_KEY``），强度见 ``researchlib.deepseek``。
当前这家凭证不可用就直接报错，不换另一家硬撑。

只用标准库发请求，HTTPS_PROXY 由 urllib 自己读取。
429、5xx 和连不上（含流读到一半的 TLS EOF）按退避间隔重试（默认 2、5、15、30 秒，可用
``LLM_RETRY_DELAYS`` 改）；超时不重试。每一次 429 打一行 ``rate-limit event=429``，方便数限流比例。
透传 ``on_text`` 时走流式 SSE（问库边写边显示），没看到 ``data: [DONE]`` 就丢掉已经拼上的半截，不当作成功。
同一个进程里同时在跑的请求有上限（``RESEARCH_LLM_CONCURRENCY``，默认 8，没有低硬顶），
问库写回答、摘要、观点、论点、识图共用。译制按同一上限夹 ``--workers``。
每次调用的最终结果记进 DATA_DIR/status/llm.json，连续失败或凭证不可用时 research-status 会列出来。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from researchlib.config import Settings
from researchlib.deepseek import DEEPSEEK_MODEL_ID, deepseek_model
from researchlib.deepseek import completion_params as deepseek_params
from researchlib.grok import GROK_MODEL_ID, retry_delays
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
# 退避间隔的默认值。改 LLM_RETRY_DELAYS（"4,8,20" 这样）就在这里换一组，译制的 chat_json 跟着一起变。
# 接口给了 Retry-After 就按它来，最多等一分钟。
RETRY_DELAYS = retry_delays({})
RETRY_AFTER_CAP = 60.0
# 思考模式把 max_tokens 花在 reasoning_content 上时，正文会是空的。
# 这时把额度翻倍再要一次；封顶 131072，识图这类长输出也不至于把容器撑爆。
EMPTY_CONTENT_MAX_TOKENS = 131_072

_slot_lock = threading.Lock()
# 接口不认 stream_options（去掉以后同一请求就成了）时记下，这个进程里后面的请求不再带，免得每张图先白挨一次 4xx。
_STREAM_USAGE = True
_slots: dict[int, threading.BoundedSemaphore] = {}

# 传输层计数：压测（research-grok-smoke --pressure）靠它看真实的重试次数和 429 次数。
# 429 在最终成功的那一次之前也会发生，只数「最后失败的请求」会得出「零限流」的错结论，所以每次
# HTTP 响应都记，重试另外单独记。只记数字和状态码，不记请求体、不记 token。
_http_lock = threading.Lock()
_http_stats: dict[str, int] = {}


def reset_http_stats() -> None:
    """清空传输层计数（压测每档开始前调一次）。"""
    with _http_lock:
        _http_stats.clear()


def http_stats() -> dict[str, int]:
    """到现在为止的传输层计数：attempts、responses、http_429、retries、http_5xx、errors、各状态码。"""
    with _http_lock:
        return dict(_http_stats)


def _note_http(name: str, amount: int = 1) -> None:
    with _http_lock:
        _http_stats[name] = _http_stats.get(name, 0) + amount


def note_rate_limit(
    *,
    provider: str,
    caller: str,
    attempt: int,
    wait_s: float,
    retry_after: bool,
    exhausted: bool,
    logger_name: str = "researchlib.llm",
) -> None:
    """记一次 429，并打一行固定格式的日志。

    成功重试之前的 429 也记。按 ``rate-limit event=429`` 数行，就是这一进程的限流次数。
    不记请求体、不记 token。
    """
    _note_http("http_429")
    logging.getLogger(logger_name).warning(
        "rate-limit event=429 provider=%s caller=%s attempt=%s wait_s=%.0f retry_after=%s exhausted=%s",
        provider,
        caller,
        attempt,
        wait_s,
        int(retry_after),
        int(exhausted),
    )


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
            "把 key 写进 .env（docker compose 的 DEEPSEEK_API_KEY）。"
            "要改走 Grok，把 RESEARCH_LLM_PROVIDER 设成 grok 或删掉这一行（默认 grok）再重启。"
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
    limit = max(1, int(getattr(settings, "llm_concurrency", 8) or 8))
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


def _content_delta(line: str) -> str:
    """一行 SSE 里新增的正文（不含思考过程）。不是数据行、解析不了都当没有。"""
    text = line.strip()
    if not text.startswith("data:"):
        return ""
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        chunk = json.loads(data)
    except ValueError:
        return ""
    if not isinstance(chunk, dict):
        return ""
    parts = []
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            parts.append(delta["content"])
    return "".join(parts)


def read_chat_completion(response, *, stream: bool, on_text: Callable[[str], None] | None = None) -> dict:
    """读完一次 chat/completions。

    流式时逐行读，看到 ``data: [DONE]`` 才把增量拼回 message。
    中途 SSL、连接重置或流提前结束都抛 IncompleteStream，调用方重试；已经读到的字不返回。
    对端若忽略 stream、回了一整段 JSON，仍按非流式解析。
    每一行的等待上限是 urlopen 的 timeout（思考模式默认 25 分钟）。
    不另设更短的空闲超时，避免把还在思考的连接当成断开再花一次。
    on_text：正文每多一截，就把到目前为止的正文交给它，只用来提前显示；它出错不影响这次读取。
    """
    if not stream:
        return json.loads(_read_body(response))
    lines: list[str] = []
    shown: list[str] = []
    try:
        for line in _iter_text_lines(response):
            if not lines and not line.strip():
                continue
            if not lines and line.lstrip().startswith("{"):
                return json.loads(line + _read_body(response))
            lines.append(line)
            if on_text is not None:
                piece = _content_delta(line)
                if piece:
                    shown.append(piece)
                    _notify(on_text, "".join(shown))
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


def _notify(on_text: Callable[[str], None], text: str) -> None:
    try:
        on_text(text)
    except Exception:  # noqa: BLE001 - 只是提前显示，显示失败不能打断这次请求
        logging.getLogger(__name__).warning("提前显示正文失败", exc_info=True)


def _post(settings: Settings, token: str, payload: dict, on_text: Callable[[str], None] | None = None) -> dict:
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
    # 透传 on_text 时（问库边写边显示）走流式：Grok 的推理增量一直往连接上写，
    # 中间设备不会把长时间没字节的连接拆掉。不传时仍一次读完的 JSON，识图、摘要、译制都走这条。
    stream = on_text is not None or str(payload.get("model", "")).startswith("deepseek")
    if stream:
        request.data = json.dumps({**payload, "stream": True}, ensure_ascii=False).encode("utf-8")
        request.add_header("Accept", "text/event-stream")
    # 套接字超时是 settings.llm_timeout：连接和随后每一次读都用它，默认 25 分钟。
    with urllib.request.urlopen(request, timeout=settings.llm_timeout) as response:
        return read_chat_completion(response, stream=stream, on_text=on_text)


def _delays() -> tuple[float, ...]:
    """这一次调用用的退避间隔（秒）。LLM_RETRY_DELAYS 改过就按新的，填坏按默认。"""
    return retry_delays()


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


def add_usage(total: dict | None, reported) -> None:
    """把接口报的一次用量加进 total：prompt、completion，以及 completion 里思考用掉的 reasoning。"""
    if total is None or not isinstance(reported, dict):
        return
    details = reported.get("completion_tokens_details")
    found = {
        "prompt": reported.get("prompt_tokens"),
        "completion": reported.get("completion_tokens"),
        "reasoning": details.get("reasoning_tokens") if isinstance(details, dict) else None,
    }
    for name, value in found.items():
        if type(value) is int and value >= 0:
            total[name] = total.get(name, 0) + value


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
    usage: dict | None = None,
    on_text: Callable[[str], None] | None = None,
    slot: threading.BoundedSemaphore | None = None,
) -> tuple[str, str]:
    """返回 (回答文本, 实际模型名)。

    Grok：grok-4.7 / reasoning_effort=high / fast=false / 至少 128000，50 万上下文。
    ``exact_max_tokens`` 会覆盖上面的下限（识图转写不需要几十万输出，否则容器 OOM）。
    ``usage``：传一个 dict 进来，返回时加上这次实际用掉的 token（prompt、completion、其中思考的 reasoning），
    正文被思考挤空、加额度再要的那一次也算。接口没报用量就不加。流式要请求里带
    ``stream_options.include_usage`` 才报，只有传了 usage 的流式调用才带；接口不认这个字段就去掉再发一次，
    去掉以后成了，这个进程后面的请求就不再带。
    ``on_text``：流式时，正文每多一截就把到目前为止的正文交给它（问库边写边显示）；重发前先交一次空串。
    返回值照旧以读完的为准。
    ``slot``：调用方自己管并发时传进来的信号量（问库分批读资料），不占摘要、观点、识图共用的那份上限。
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
    streaming = on_text is not None or str(payload.get("model", "")).startswith("deepseek")
    if usage is not None and _STREAM_USAGE and streaming:
        payload["stream_options"] = {"include_usage": True}
    delays = _delays()
    try:
        token = _oauth_token(settings) if provider == PROVIDER_GROK else _deepseek_token()
    except LLMUnavailable as exc:
        raise _failed(settings, exc, provider=label) from None
    body = None
    refreshed = False
    retries = 0
    budget_bumped = False
    dropped_stream_usage = False
    attempts = 0
    while True:
        if on_text is not None and attempts:
            # 上一次读到一半断了或要重发：先把已经显示的半截收回。
            _notify(on_text, "")
        attempts += 1
        _note_http("attempts")
        try:
            with slot or _slot(settings):
                if on_text is None:
                    body = _post(settings, token, payload)
                else:
                    body = _post(settings, token, payload, on_text=on_text)
        except urllib.error.HTTPError as exc:
            _note_http("responses")
            _note_http(f"status_{exc.code}")
            if 500 <= exc.code < 600:
                _note_http("http_5xx")
            detail = exc.read().decode("utf-8", "replace")[:300]
            if provider == PROVIDER_GROK and not refreshed and is_stale_xai_access(exc.code, detail):
                # 过期的 access token 被报成 401/403：强制刷新一次再试，不算在重试次数里。
                refreshed = True
                try:
                    token = _oauth_token(settings, force=True, discard=token)
                except LLMUnavailable as unavailable:
                    raise _failed(settings, unavailable, provider=label) from None
                continue
            if exc.code in (400, 422) and "stream_options" in payload:
                # 只为报用量加的字段。接口或转发不认它时报错的说法不一，4xx 一律去掉再发一次，这次不要用量；
                # 别的原因的 4xx 再发一次还是同样的错，照常报出去（4xx 不计费）。
                payload.pop("stream_options")
                dropped_stream_usage = True
                continue
            will_retry = exc.code in RETRY_STATUS and retries < len(delays)
            if exc.code == 429:
                # 429 在最终成功的那一次之前也会发生：每一次都记，不只记最后失败的那次。
                hinted = _retry_after(exc)
                wait = (hinted if hinted is not None else delays[retries]) if will_retry else 0.0
                note_rate_limit(
                    provider=label,
                    caller="chat",
                    attempt=retries + 1,
                    wait_s=wait,
                    retry_after=hinted is not None,
                    exhausted=not will_retry,
                )
            if will_retry:
                _note_http("retries")
                time.sleep(_retry_after(exc) or delays[retries])
                retries += 1
                continue
            raise _failed(settings, LLMError(f"{label} 接口返回 {exc.code}：{detail}"), provider=label) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as exc:
            if not _is_timeout(exc) and retries < len(delays):
                _note_http("retries")
                _note_http("connect_errors")
                time.sleep(delays[retries])
                retries += 1
                continue
            _note_http("errors")
            raise _failed(settings, LLMError(f"连不上 {label} 接口：{exc}"), provider=label) from exc
        except json.JSONDecodeError as exc:
            _note_http("errors")
            raise _failed(settings, LLMError(f"{label} 接口返回的不是 JSON"), provider=label) from exc
        _note_http("responses")
        add_usage(usage, body.get("usage") if isinstance(body, dict) else None)
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
    if dropped_stream_usage:
        _forget_stream_usage()
    record_llm(settings.data_dir, ok=True, provider=label)
    return text, str(body.get("model") or payload["model"])


def _forget_stream_usage() -> None:
    """接口不认 stream_options：这个进程后面的请求不再带，识图报告会写「接口这次没报用量」。"""
    global _STREAM_USAGE
    _STREAM_USAGE = False

"""调用 Grok（xAI 的 chat/completions）。

鉴权只认与 AIHOT 共用的 xAI OAuth。Docker 默认
``XAI_OAUTH_TOKEN_PATH=/aihot-data/secrets/xai-oauth.json``。
登录在 AIHOT 侧做；``research-xai-login status`` 只检查同一份文件。
没有这份登录时直接报错，不退回 ``XAI_API_KEY`` 或其他 API key。

强度与译制共用 ``researchlib.grok``：grok-4.7、reasoning_effort=xhigh、
输出预算不低于 128000、上下文按 grok-4.7 的 50 万 token，不发送 fast。

只用标准库发请求，HTTPS_PROXY 由 urllib 自己读取。
429、5xx 和连不上这类临时错误退避重试两次；超时不重试（一次已经等了很久）。同一个进程里同时在跑的
请求有上限（RESEARCH_LLM_CONCURRENCY，默认 3），问库分批、摘要、观点、论点共用。
每次调用的最终结果记进 DATA_DIR/status/llm.json，连续失败或登录失效时首页提示。
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
from researchlib.grok import completion_params
from researchlib.health import record_llm
from researchlib.translate.xai_oauth import (
    XaiLoginRequiredError,
    get_xai_access_token,
    is_stale_xai_access,
    xai_oauth_status,
)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# 第一次重试前等 5 秒，第二次等 20 秒。接口给了 Retry-After 就按它来，最多等一分钟。
RETRY_DELAYS = (5.0, 20.0)
RETRY_AFTER_CAP = 60.0

_slot_lock = threading.Lock()
_slots: dict[int, threading.BoundedSemaphore] = {}


class LLMUnavailable(Exception):
    """没有可用的 xAI OAuth。页面退化为只给检索结果。"""


class LLMError(Exception):
    """接口返回错误或格式不对。"""


def credential_kind(settings: Settings) -> str:
    """"oauth" 或 ""。不认 API key。"""
    if xai_oauth_status(settings.data_dir).logged_in:
        return "oauth"
    return ""


def llm_configured(settings: Settings) -> bool:
    return credential_kind(settings) == "oauth"


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


def ensure_oauth(settings: Settings) -> None:
    """命令行入口在开工前调用。没有共用 OAuth 就抛 LLMUnavailable，不退回 API key。"""
    _oauth_token(settings)


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


def _failed(settings: Settings, exc: Exception) -> Exception:
    record_llm(getattr(settings, "data_dir", None), ok=False, error=str(exc),
               unavailable=isinstance(exc, LLMUnavailable))
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
    with urllib.request.urlopen(request, timeout=settings.llm_timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def chat(
    settings: Settings,
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.2,
) -> tuple[str, str]:
    """返回 (回答文本, 实际模型名)。强度固定为 grok-4.7 / xhigh / 至少 128000，不发 fast。"""
    payload = {
        **completion_params(temperature=temperature, max_tokens=max_tokens),
        "messages": messages,
    }
    try:
        token = _oauth_token(settings)
    except LLMUnavailable as exc:
        raise _failed(settings, exc) from None
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
            if not refreshed and is_stale_xai_access(exc.code, detail):
                # 过期的 access token 被报成 401/403：强制刷新一次再试，不算在重试次数里。
                refreshed = True
                try:
                    token = _oauth_token(settings, force=True, discard=token)
                except LLMUnavailable as unavailable:
                    raise _failed(settings, unavailable) from None
                continue
            if exc.code in RETRY_STATUS and retries < len(RETRY_DELAYS):
                time.sleep(_retry_after(exc) or RETRY_DELAYS[retries])
                retries += 1
                continue
            raise _failed(settings, LLMError(f"Grok 接口返回 {exc.code}：{detail}")) from exc
        except (urllib.error.URLError, ConnectionError, http.client.HTTPException, TimeoutError) as exc:
            if not _is_timeout(exc) and retries < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[retries])
                retries += 1
                continue
            raise _failed(settings, LLMError(f"连不上 Grok 接口：{exc}")) from exc
        except json.JSONDecodeError as exc:
            raise _failed(settings, LLMError("Grok 接口返回的不是 JSON")) from exc
    try:
        text = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise _failed(settings, LLMError("Grok 接口返回里没有 choices[0].message.content")) from exc
    record_llm(settings.data_dir, ok=True)
    return text.strip(), str(body.get("model") or payload["model"])

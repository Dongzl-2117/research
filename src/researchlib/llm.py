"""调用 Grok（xAI 的 OpenAI 兼容 chat/completions）。

凭证按顺序取：
1. 与 AIHOT 共用的 xAI OAuth。Docker 默认
   ``XAI_OAUTH_TOKEN_PATH=/aihot-data/secrets/xai-oauth.json``。
   登录在 AIHOT 侧做；``research-xai-login status`` 只检查同一份文件。
2. 可选的 ``XAI_API_KEY``。没有共用登录时，问库和摘要才退回控制台 key。

两者都是 ``Authorization: Bearer``。只用标准库发请求，HTTPS_PROXY 由 urllib 自己读取。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from researchlib.config import Settings
from researchlib.translate.xai_oauth import (
    XaiLoginRequiredError,
    get_xai_access_token,
    is_stale_xai_access,
    xai_oauth_status,
)


class LLMUnavailable(Exception):
    """没有可用凭证。页面退化为只给检索结果。"""


class LLMError(Exception):
    """接口返回错误或格式不对。"""


def credential_kind(settings: Settings) -> str:
    """"oauth"、"key" 或 ""。"""
    if xai_oauth_status(settings.data_dir).logged_in:
        return "oauth"
    if settings.llm_api_key:
        return "key"
    return ""


def llm_configured(settings: Settings) -> bool:
    return credential_kind(settings) != ""


def _token(settings: Settings, kind: str, *, force: bool = False, discard: str | None = None) -> str:
    if kind == "oauth":
        try:
            return get_xai_access_token(
                settings.data_dir,
                force=force,
                discard_access_token=discard,
            )
        except XaiLoginRequiredError as exc:
            if settings.llm_api_key:
                return settings.llm_api_key
            raise LLMUnavailable(str(exc)) from exc
    if kind == "key":
        return settings.llm_api_key
    raise LLMUnavailable("没有 xAI 凭证。请在 AIHOT 侧登录共用 OAuth，或设置 XAI_API_KEY。")


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
    """返回 (回答文本, 实际模型名)。"""
    kind = credential_kind(settings)
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        # 推理和答案共用这笔预算，给小了推理会把答案挤掉。
        "max_tokens": max(max_tokens or 0, settings.llm_max_tokens),
        "temperature": temperature,
    }
    if settings.llm_reasoning_effort:
        payload["reasoning_effort"] = settings.llm_reasoning_effort
    token = _token(settings, kind)
    body = None
    for attempt in range(2):
        try:
            body = _post(settings, token, payload)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if attempt == 0 and kind == "oauth" and is_stale_xai_access(exc.code, detail):
                token = _token(settings, kind, force=True, discard=token)
                continue
            raise LLMError(f"Grok 接口返回 {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"连不上 Grok 接口：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError("Grok 接口返回的不是 JSON") from exc
    try:
        text = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("Grok 接口返回里没有 choices[0].message.content") from exc
    return text.strip(), str(body.get("model") or settings.llm_model)

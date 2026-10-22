"""站内调 LLM 做译制：默认 DeepSeek，Grok 作为显式选项。

默认 DeepSeek：deepseek-v4-pro + thinking + reasoning_effort=max + 输出至少 384000，
上下文 1M，不发 temperature，鉴权用 DEEPSEEK_API_KEY 的 Bearer。
``RESEARCH_LLM_PROVIDER=grok`` 时走 xAI OAuth（grok-4.7 + xhigh + 至少 128000，不开 fast）。
模型、推理强度、输出地板和超时与问库、摘要共用 researchlib.deepseek / researchlib.grok。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
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


@dataclass
class ChatJsonResult:
    data: dict[str, Any]
    model: str
    usage: dict[str, Any] | None
    latency_ms: int


def output_tokens(requested: int | None = None) -> int:
    """当前提供方的输出预算。DeepSeek 地板 384000，Grok 128000。"""
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
        raise RuntimeError(
            "没有配置 DEEPSEEK_API_KEY，DeepSeek 译不了。"
            "把 key 写进 .env（compose 的 DEEPSEEK_API_KEY），或者设 RESEARCH_LLM_PROVIDER=grok 用回 Grok OAuth。"
        )
    return key


def _extract_json(text: str) -> Any:
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
    """POST {base}/chat/completions。强度按当前提供方，DeepSeek 默认 max / 384k / thinking。"""
    del purpose, subject  # 保留形参便于日后打回执；当前不落库
    provider = llm_provider()
    label = provider_label(provider)
    endpoint = f"{llm_base_url()}/chat/completions"
    user_text = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
    params = _params(temperature, max_tokens)
    body = {
        **params,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_object"},
    }
    payload = json.dumps(body).encode("utf-8")
    timeout = timeout_s if timeout_s is not None else long_timeout_s()
    bearer = _bearer(data_dir)

    def post(token: str) -> tuple[int, str]:
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    started = time.time()
    status, text = post(bearer)
    if provider == PROVIDER_GROK and status >= 400 and is_stale_xai_access(status, text):
        bearer = get_xai_access_token(data_dir, force=True, discard_access_token=bearer)
        status, text = post(bearer)
    if status >= 400:
        raise RuntimeError(f"{label} HTTP {status}: {text[:500]}")
    try:
        response = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} 返回非 JSON：{text[:300]}") from exc
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    # 思考过程在 reasoning_content，只取最终答案。
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    data = _extract_json(content)
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} JSON 根节点不是对象")
    return ChatJsonResult(
        data=data,
        model=str(response.get("model") or params["model"]),
        usage=response.get("usage") if isinstance(response.get("usage"), dict) else None,
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
    "XaiLoginRequiredError",
    "chat_json",
    "grok_output_tokens",
    "long_timeout_s",
    "model_id",
    "output_tokens",
]

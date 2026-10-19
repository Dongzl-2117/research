"""站内调 Grok：grok-4.7 + reasoning_effort=xhigh + 大上下文，不开 fast。

对齐 AIHOT providers/llm.ts 与 llm-limits.ts 的 Grok 路径。
鉴权走 xAI OAuth access token（Bearer），过期自动刷新。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from researchlib.translate.xai_oauth import (
    XAI_INFERENCE_BASE_URL,
    XaiLoginRequiredError,
    get_xai_access_token,
    is_stale_xai_access,
)

GROK_MODEL_ID = "grok-4.7"
GROK_REASONING_EFFORT = "xhigh"
GROK_CONTEXT_TOKENS = 500_000
GROK_OUTPUT_FLOOR = 128_000
GROK_INPUT_CHARS = 360_000
LONG_LLM_TIMEOUT_S = 1_500  # 25 分钟，盖住 xhigh 推理


@dataclass
class ChatJsonResult:
    data: dict[str, Any]
    model: str
    usage: dict[str, Any] | None
    latency_ms: int


def grok_output_tokens(requested: int | None = None) -> int:
    dedicated = os.environ.get("XAI_MAX_TOKENS", "").strip()
    if dedicated.isdigit():
        return int(dedicated)
    raised = os.environ.get("LLM_MAX_TOKENS", "").strip()
    raised_n = int(raised) if raised.isdigit() else 0
    return max(raised_n, requested or 0, GROK_OUTPUT_FLOOR)


def long_timeout_s() -> float:
    raw = os.environ.get("LLM_LONG_TIMEOUT_MS", "").strip()
    if raw.isdigit():
        return max(60.0, int(raw) / 1000)
    return float(LONG_LLM_TIMEOUT_S)


def _base_url() -> str:
    return (os.environ.get("XAI_BASE_URL", "").strip() or XAI_INFERENCE_BASE_URL).rstrip("/")


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
    """POST /v1/chat/completions，固定 grok-4.7 / xhigh / 至少 128k max_tokens。"""
    del purpose, subject  # 保留形参便于日后打回执；当前不落库
    endpoint = f"{_base_url()}/chat/completions"
    user_text = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
    body = {
        "model": GROK_MODEL_ID,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "reasoning_effort": GROK_REASONING_EFFORT,
        "max_tokens": grok_output_tokens(max_tokens),
    }
    payload = json.dumps(body).encode("utf-8")
    timeout = timeout_s if timeout_s is not None else long_timeout_s()
    bearer = get_xai_access_token(data_dir)

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
    if status >= 400 and is_stale_xai_access(status, text):
        bearer = get_xai_access_token(data_dir, force=True, discard_access_token=bearer)
        status, text = post(bearer)
    if status >= 400:
        raise RuntimeError(f"Grok HTTP {status}: {text[:500]}")
    try:
        response = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Grok 返回非 JSON：{text[:300]}") from exc
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    data = _extract_json(content)
    if not isinstance(data, dict):
        raise RuntimeError("Grok JSON 根节点不是对象")
    return ChatJsonResult(
        data=data,
        model=GROK_MODEL_ID,
        usage=response.get("usage") if isinstance(response.get("usage"), dict) else None,
        latency_ms=int((time.time() - started) * 1000),
    )


__all__ = [
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
]

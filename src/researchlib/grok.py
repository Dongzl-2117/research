"""站内 Grok 的固定强度。译制、问库、摘要、观点、论点共用这一组，避免两套参数以后漂移。

鉴权不在这里。调用方只拿 xAI OAuth access token（与 AIHOT 共用的 xai-oauth.json）。
模型固定 grok-4.7，reasoning_effort=xhigh，输出预算不低于 128000。
上下文用 grok-4.7 自带的 50 万 token，请求里不另传更小的上限，也不发送 fast。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

GROK_MODEL_ID = "grok-4.7"
GROK_REASONING_EFFORT = "xhigh"
GROK_CONTEXT_TOKENS = 500_000
GROK_OUTPUT_FLOOR = 128_000
GROK_INPUT_CHARS = 360_000
LONG_LLM_TIMEOUT_S = 1_500  # 25 分钟，盖住 xhigh 推理

# 这些字段会把上下文或速度从上面的口径拉开，请求里不能出现。
_FORBIDDEN_FIELDS = frozenset(
    {
        "fast",
        "context_length",
        "context_window",
        "max_context_tokens",
        "max_input_tokens",
    }
)


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def grok_output_tokens(requested: int | None = None, environ: Mapping[str, str] | None = None) -> int:
    """输出 token 上限。XAI_MAX_TOKENS / LLM_MAX_TOKENS 只能抬高，不能低于 128000。"""
    env = _env(environ)
    dedicated = env.get("XAI_MAX_TOKENS", "").strip()
    if dedicated.isdigit():
        chosen = int(dedicated)
    else:
        raised = env.get("LLM_MAX_TOKENS", "").strip()
        chosen = int(raised) if raised.isdigit() else 0
    return max(chosen, requested or 0, GROK_OUTPUT_FLOOR)


def long_timeout_s(environ: Mapping[str, str] | None = None) -> float:
    """长调用超时（秒）。LLM_LONG_TIMEOUT_MS 是译制和问库共用的下限；RESEARCH_LLM_TIMEOUT 更短时抬上来。"""
    env = _env(environ)
    raw = env.get("LLM_LONG_TIMEOUT_MS", "").strip()
    if raw.isdigit():
        floor = max(60.0, int(raw) / 1000)
    else:
        floor = float(LONG_LLM_TIMEOUT_S)
    asked = env.get("RESEARCH_LLM_TIMEOUT", "").strip()
    if not asked:
        return floor
    try:
        return max(floor, float(asked))
    except ValueError:
        return floor


def completion_params(
    *,
    temperature: float,
    max_tokens: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """chat/completions 里和强度有关的字段。不含 fast，也不另传更小的上下文。"""
    if "fast" in GROK_MODEL_ID:
        raise RuntimeError(f"拒绝 fast 模型：{GROK_MODEL_ID}")
    params = {
        "model": GROK_MODEL_ID,
        "temperature": temperature,
        "reasoning_effort": GROK_REASONING_EFFORT,
        "max_tokens": grok_output_tokens(max_tokens, environ),
    }
    forbidden = _FORBIDDEN_FIELDS.intersection(params)
    if forbidden or "fast" in str(params["model"]):
        raise RuntimeError(f"Grok 请求不能带 {', '.join(sorted(forbidden)) or 'fast'}")
    return params

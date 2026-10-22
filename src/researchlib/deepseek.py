"""站内 DeepSeek 的固定强度。译制、问库、摘要、观点、论点共用这一组。

模型固定 deepseek-v4-pro（旗舰档，不用 flash），thinking 打开、reasoning_effort=max，
输出预算不低于 384000，上下文按 1M token。思考模式下 temperature、presence_penalty、
frequency_penalty 不起作用，请求里一律不发，也不发 fast。

鉴权不在这里，由 researchlib.llm / researchlib.translate.llm 按 DEEPSEEK_API_KEY 加 Bearer。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from researchlib.llm_provider import DEEPSEEK_DEFAULT_BASE_URL

DEEPSEEK_BASE_URL = DEEPSEEK_DEFAULT_BASE_URL
DEEPSEEK_MODEL_ID = "deepseek-v4-pro"
DEEPSEEK_REASONING_EFFORT = "max"
DEEPSEEK_CONTEXT_TOKENS = 1_000_000
DEEPSEEK_OUTPUT_FLOOR = 384_000

_ALLOWED_EFFORT = frozenset({"low", "high", "max"})

# 这些字段会把强度或上下文从上面的口径拉开，请求里不能出现。
_FORBIDDEN_FIELDS = frozenset(
    {
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "fast",
        "context_length",
        "context_window",
        "max_context_tokens",
        "max_input_tokens",
    }
)


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def deepseek_model(environ: Mapping[str, str] | None = None) -> str:
    """DEEPSEEK_MODEL 只能换成别的旗舰，flash 档一律拒绝。"""
    env = _env(environ)
    model = (env.get("DEEPSEEK_MODEL", "").strip() or DEEPSEEK_MODEL_ID)
    if "flash" in model.lower():
        raise RuntimeError(f"站内不用 flash 档：{model}。DeepSeek 固定 {DEEPSEEK_MODEL_ID}。")
    return model


def deepseek_reasoning_effort(environ: Mapping[str, str] | None = None) -> str:
    """DEEPSEEK_REASONING_EFFORT，只认 low / high / max；xhigh 按 high，其余回到 max。"""
    raw = (_env(environ).get("DEEPSEEK_REASONING_EFFORT") or "").strip().lower()
    if raw in _ALLOWED_EFFORT:
        return raw
    if raw == "xhigh":
        return "high"
    return DEEPSEEK_REASONING_EFFORT


def deepseek_output_tokens(requested: int | None = None, environ: Mapping[str, str] | None = None) -> int:
    """输出 token 上限。DEEPSEEK_MAX_TOKENS / LLM_MAX_TOKENS 只能抬高，不能低于 384000。"""
    env = _env(environ)
    dedicated = env.get("DEEPSEEK_MAX_TOKENS", "").strip()
    if dedicated.isdigit():
        chosen = int(dedicated)
    else:
        raised = env.get("LLM_MAX_TOKENS", "").strip()
        chosen = int(raised) if raised.isdigit() else 0
    return max(chosen, requested or 0, DEEPSEEK_OUTPUT_FLOOR)


def completion_params(
    *,
    max_tokens: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """chat/completions 里和强度有关的字段。不带 temperature，也不另传更小的上下文。"""
    params = {
        "model": deepseek_model(environ),
        "max_tokens": deepseek_output_tokens(max_tokens, environ),
        "reasoning_effort": deepseek_reasoning_effort(environ),
        "thinking": {"type": "enabled"},
    }
    forbidden = _FORBIDDEN_FIELDS.intersection(params)
    if forbidden:
        raise RuntimeError(f"DeepSeek 请求不能带 {', '.join(sorted(forbidden))}")
    if "flash" in str(params["model"]).lower():
        raise RuntimeError(f"站内不用 flash 档：{params['model']}")
    return params

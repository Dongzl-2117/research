"""站内 Grok 的固定强度。译制、问库、摘要、观点、论点、识图共用这一组，避免两套参数以后漂移。

鉴权不在这里。调用方只拿 xAI OAuth access token（researchlib.translate.xai_oauth）。

口径（2026-11-10 按 ``GET /v1/models/grok-4.7`` 核过）：

- 模型固定 grok-4.7，不带 ``fast`` 后缀；请求里显式发 ``"fast": false``，不开快速档。
- ``reasoning_effort=high``。xAI 报的能力是 ``low/medium/high/xhigh``，默认 high；owner 要 high，不是 xhigh。
- 上下文用模型自报的最大窗口 500000（``context_length``），请求里不另传更小的上限。
- 输出预算不低于 128000。

并发上限（``grok_concurrency``）是同一个进程里同时在跑的 LLM 请求数。译制和识图打的是同一份
账号额度，两边都读 ``RESEARCH_LLM_CONCURRENCY``（默认 8）。不设低硬顶：填 40 就是 40。
想再封一层顶，显式设 ``GROK_MAX_CONCURRENCY``；不设就不夹。11-09 以前 DeepSeek 开 40 路时容器被
OOM 杀过（EXIT=137），往上加之前先跑 ``research-grok-smoke --pressure``，看 429 日志和内存。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

GROK_MODEL_ID = "grok-4.7"
GROK_REASONING_EFFORT = "high"
GROK_FAST = False
GROK_CONTEXT_TOKENS = 500_000
GROK_OUTPUT_FLOOR = 128_000
GROK_INPUT_CHARS = 360_000
LONG_LLM_TIMEOUT_S = 1_500  # 25 分钟，盖住 high 推理的长文

# 同一个进程里同时在跑的 LLM 请求数。默认 8；RESEARCH_LLM_CONCURRENCY 直接生效，没有 24 这种低硬顶。
# 只有显式设置的 GROK_MAX_CONCURRENCY 才会再往下夹。
GROK_CONCURRENCY_DEFAULT = 8

# 429 / 5xx / 连接被重置时的退避间隔（秒）。LLM_RETRY_DELAYS 可以改，最多 6 档，每档 0～600。
RETRY_DELAYS_DEFAULT = (2.0, 5.0, 15.0, 30.0)
RETRY_DELAYS_MAX_STEPS = 6
RETRY_DELAY_MAX_S = 600.0

# 这些字段会把上下文或速度从上面的口径拉开，请求里不能出现。
_FORBIDDEN_FIELDS = frozenset(
    {
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


def _asked_concurrency(environ: Mapping[str, str] | None) -> int:
    """``RESEARCH_LLM_CONCURRENCY``。空着或填坏用默认 8；0 和负数抬到 1。"""
    raw = _env(environ).get("RESEARCH_LLM_CONCURRENCY", "").strip()
    try:
        asked = int(raw) if raw else GROK_CONCURRENCY_DEFAULT
    except ValueError:
        asked = GROK_CONCURRENCY_DEFAULT
    return max(1, asked)


def grok_concurrency_ceiling(environ: Mapping[str, str] | None = None) -> int | None:
    """可选上限 ``GROK_MAX_CONCURRENCY``。不设、填坏、小于 1 都表示不设上限。"""
    raw = _env(environ).get("GROK_MAX_CONCURRENCY", "").strip()
    if not raw:
        return None
    try:
        ceiling = int(raw)
    except ValueError:
        return None
    if ceiling < 1:
        return None
    return ceiling


def grok_concurrency(environ: Mapping[str, str] | None = None) -> int:
    """同一个进程里同时在跑的 LLM 请求数。

    ``RESEARCH_LLM_CONCURRENCY`` 直接生效，没有默认的低硬顶。设了 ``GROK_MAX_CONCURRENCY``
    才按它往下夹。译制、识图、问库、摘要、观点、论点都读这个数。
    """
    asked = _asked_concurrency(environ)
    ceiling = grok_concurrency_ceiling(environ)
    if ceiling is None:
        return asked
    return min(asked, ceiling)


def grok_concurrency_plan(environ: Mapping[str, str] | None = None) -> dict:
    """asked（环境变量要的）、ceiling（可选上限，不设是 None）、effective（实际生效的）。

    clamped 为真表示被 ``GROK_MAX_CONCURRENCY`` 夹住了，报告里要写明。
    """
    asked = _asked_concurrency(environ)
    ceiling = grok_concurrency_ceiling(environ)
    effective = asked if ceiling is None else min(asked, ceiling)
    return {
        "asked": asked,
        "ceiling": ceiling,
        "effective": effective,
        "clamped": ceiling is not None and asked > effective,
    }


def retry_delays(environ: Mapping[str, str] | None = None) -> tuple[float, ...]:
    """退避间隔。默认 2、5、15、30 秒；LLM_RETRY_DELAYS="4,8,20" 这样改。

    接口给了 Retry-After 时按它来（另有一分钟的上限），这里管的是没给的时候。
    填坏、填空都按默认来，不让一个环境变量把重试关掉。
    """
    env = _env(environ)
    raw = env.get("LLM_RETRY_DELAYS", "").strip()
    if not raw:
        return RETRY_DELAYS_DEFAULT
    parts = [piece.strip() for piece in raw.split(",") if piece.strip()]
    values: list[float] = []
    for piece in parts[:RETRY_DELAYS_MAX_STEPS]:
        try:
            seconds = float(piece)
        except ValueError:
            return RETRY_DELAYS_DEFAULT
        if seconds < 0 or seconds > RETRY_DELAY_MAX_S:
            return RETRY_DELAYS_DEFAULT
        values.append(seconds)
    return tuple(values) or RETRY_DELAYS_DEFAULT


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
    """chat/completions 里和强度有关的字段。显式 fast=false，不另传更小的上下文。"""
    if "fast" in GROK_MODEL_ID:
        raise RuntimeError(f"拒绝 fast 模型：{GROK_MODEL_ID}")
    params = {
        "model": GROK_MODEL_ID,
        "temperature": temperature,
        "reasoning_effort": GROK_REASONING_EFFORT,
        "fast": GROK_FAST,
        "max_tokens": grok_output_tokens(max_tokens, environ),
    }
    forbidden = _FORBIDDEN_FIELDS.intersection(params)
    if forbidden or params["fast"] is not GROK_FAST or "fast" in str(params["model"]):
        raise RuntimeError(f"Grok 请求不能带 {', '.join(sorted(forbidden)) or 'fast 模式'}")
    return params

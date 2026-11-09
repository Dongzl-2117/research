"""站内 Grok 的固定强度。译制、问库、摘要、观点、论点、识图共用这一组，避免两套参数以后漂移。

鉴权不在这里。调用方只拿 xAI OAuth access token（researchlib.translate.xai_oauth）。

口径（2026-11-10 按 ``GET /v1/models/grok-4.7`` 核过）：

- 模型固定 grok-4.7，不带 ``fast`` 后缀；请求里显式发 ``"fast": false``，不开快速档。
- ``reasoning_effort=high``。xAI 报的能力是 ``low/medium/high/xhigh``，默认 high；owner 要 high，不是 xhigh。
- 上下文用模型自报的最大窗口 500000（``context_length``），请求里不另传更小的上限。
- 输出预算不低于 128000。

并发上限（``grok_concurrency``）是同一个进程里同时在跑的 Grok 请求数，问库、摘要、观点、论点、识图、
译制共用一份。默认 8，硬顶由 ``GROK_MAX_CONCURRENCY`` 决定（不设时 24）：11-09 以前 DeepSeek 开 40 路时
容器被 OOM 杀过（EXIT=137），Grok 走一次读完的 JSON，单请求占用随输出长度涨，档位越高越要先压测再往上调。
硬顶可配只是为了「按实测调上去」：调之前先跑 ``research-grok-smoke --pressure``，把 asked（要的档位）和
effective（实际同时在跑的请求数）分别记下来，也把容器内存峰值记下来。上限再多也不超过
``GROK_CONCURRENCY_ABSOLUTE_LIMIT``，那是防手滑的保险，不是压测过的档位。
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

# 同一个进程里同时在跑的 Grok 请求上限：默认值、硬顶的默认值、以及填什么都不越过的保险线。
# 硬顶用 GROK_MAX_CONCURRENCY 改（压测过再往上调），RESEARCH_LLM_CONCURRENCY 在这个区间里取。
GROK_CONCURRENCY_DEFAULT = 8
GROK_CONCURRENCY_CEILING = 24
GROK_CONCURRENCY_ABSOLUTE_LIMIT = 64

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


def grok_concurrency_ceiling(environ: Mapping[str, str] | None = None) -> int:
    """并发硬顶：``GROK_MAX_CONCURRENCY``，不设时 24。

    填坏按默认，填得比 ``GROK_CONCURRENCY_ABSOLUTE_LIMIT`` 大按保险线算。压测过哪一档就把硬顶调到哪一档，
    不要只在命令上写一个更大的数——生产进程读的是这个值。
    """
    env = _env(environ)
    raw = env.get("GROK_MAX_CONCURRENCY", "").strip()
    try:
        ceiling = int(raw) if raw else GROK_CONCURRENCY_CEILING
    except ValueError:
        ceiling = GROK_CONCURRENCY_CEILING
    return max(1, min(ceiling, GROK_CONCURRENCY_ABSOLUTE_LIMIT))


def grok_concurrency(environ: Mapping[str, str] | None = None) -> int:
    """同一个进程里同时在跑的 Grok 请求数：``RESEARCH_LLM_CONCURRENCY``，夹在 1 到硬顶之间。

    填得比硬顶还高时按硬顶算，不因为一个环境变量把容器撑爆。想看「要了多少、实际多少」用
    ``grok_concurrency_plan``。
    """
    ceiling = grok_concurrency_ceiling(environ)
    raw = _env(environ).get("RESEARCH_LLM_CONCURRENCY", "").strip()
    try:
        asked = int(raw) if raw else GROK_CONCURRENCY_DEFAULT
    except ValueError:
        asked = GROK_CONCURRENCY_DEFAULT
    return max(1, min(asked, ceiling))


def grok_concurrency_plan(environ: Mapping[str, str] | None = None) -> dict:
    """报给压测和状态页看的三个数：asked（环境变量要的）、ceiling（硬顶）、effective（实际生效的）。

    effective 比 asked 小就是被硬顶夹住了，报告里要写明，不能拿夹过的档位冒充实测档位。
    """
    ceiling = grok_concurrency_ceiling(environ)
    effective = grok_concurrency(environ)
    raw = _env(environ).get("RESEARCH_LLM_CONCURRENCY", "").strip()
    try:
        asked = int(raw) if raw else GROK_CONCURRENCY_DEFAULT
    except ValueError:
        asked = GROK_CONCURRENCY_DEFAULT
    if asked < 1:
        asked = 1
    return {"asked": asked, "ceiling": ceiling, "effective": effective, "clamped": asked > effective}


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

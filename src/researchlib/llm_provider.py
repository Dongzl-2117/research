"""站内 LLM 用哪一家：全部走 Grok OAuth。

2026-11-10 owner 授权站内所有 LLM（译制、问库、摘要、观点、论点、识图、夜间任务）改用 Grok OAuth，
模型 grok-4.7。**不设、填错、留着以前的 ``deepseek`` 都按 grok 走**，所以宿主机 .env 里遗留的
``RESEARCH_LLM_PROVIDER=deepseek`` 不会把站点悄悄留在 DeepSeek 上。

``RESEARCH_LLM_PROVIDER=deepseek-legacy`` 是唯一还认的旧值，只在应急回退和按提供方分别测传输时用；
正常部署不设它。调用失败时不会在两个提供方之间自动切换（本来也没有切换代码），
Grok 凭证不可用就直接报错。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

PROVIDER_GROK = "grok"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_DEEPSEEK_LEGACY = "deepseek-legacy"
DEFAULT_PROVIDER = PROVIDER_GROK
PROVIDERS = (PROVIDER_GROK, PROVIDER_DEEPSEEK)

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
GROK_DEFAULT_BASE_URL = "https://api.x.ai/v1"

_LABELS = {PROVIDER_DEEPSEEK: "DeepSeek", PROVIDER_GROK: "Grok"}


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def llm_provider(environ: Mapping[str, str] | None = None) -> str:
    """当前提供方。只有显式写 deepseek-legacy 才走 DeepSeek，其余一律 grok。"""
    raw = (_env(environ).get("RESEARCH_LLM_PROVIDER") or "").strip().lower()
    return PROVIDER_DEEPSEEK if raw == PROVIDER_DEEPSEEK_LEGACY else PROVIDER_GROK


def provider_label(provider: str) -> str:
    """页面上、报错里的叫法。"""
    return _LABELS.get(provider, "LLM")


def llm_base_url(environ: Mapping[str, str] | None = None) -> str:
    """OpenAI 兼容的 base URL，不带 /chat/completions。"""
    env = _env(environ)
    if llm_provider(env) == PROVIDER_GROK:
        return (env.get("XAI_BASE_URL", "").strip() or GROK_DEFAULT_BASE_URL).rstrip("/")
    return (env.get("DEEPSEEK_BASE_URL", "").strip() or DEEPSEEK_DEFAULT_BASE_URL).rstrip("/")


def deepseek_api_key(environ: Mapping[str, str] | None = None) -> str:
    return (_env(environ).get("DEEPSEEK_API_KEY") or "").strip()

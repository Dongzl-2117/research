"""站内 LLM 用哪一家：默认 DeepSeek，Grok 要显式打开。

``RESEARCH_LLM_PROVIDER=deepseek|grok``，大小写和首尾空格都不计较。不设、或者填了不认识的值，
一律按 deepseek——这是 owner 2026-10-22 定的默认，等他说换回 Grok 再改。
Grok 路径仍然是那份与 AIHOT 共用的 xAI OAuth，只是不再是默认。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_GROK = "grok"
DEFAULT_PROVIDER = PROVIDER_DEEPSEEK
PROVIDERS = (PROVIDER_DEEPSEEK, PROVIDER_GROK)

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
GROK_DEFAULT_BASE_URL = "https://api.x.ai/v1"

_LABELS = {PROVIDER_DEEPSEEK: "DeepSeek", PROVIDER_GROK: "Grok"}


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def llm_provider(environ: Mapping[str, str] | None = None) -> str:
    """当前提供方。没设或填错都按 deepseek，Grok 只认明确的 grok。"""
    raw = (_env(environ).get("RESEARCH_LLM_PROVIDER") or "").strip().lower()
    return PROVIDER_GROK if raw == PROVIDER_GROK else PROVIDER_DEEPSEEK


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

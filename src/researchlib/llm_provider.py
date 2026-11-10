"""站内 LLM 用哪一家，由 ``RESEARCH_LLM_PROVIDER`` 切换。

取值（大小写和两端空白不计）：

- 不设、空、``grok``，以及不认识的值：Grok OAuth。这是默认。
- ``deepseek``：DeepSeek。``deepseek-legacy`` 是同一个值的别名，以前的应急写法还能用。

改这一行再重启进程就换提供方，不用改代码。调用失败时不会在两家之间自动切换：
当前这家凭证不可用就直接报错。
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
_DEEPSEEK_VALUES = frozenset({PROVIDER_DEEPSEEK, PROVIDER_DEEPSEEK_LEGACY})


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def llm_provider(environ: Mapping[str, str] | None = None) -> str:
    """当前提供方。默认 grok；显式 ``deepseek``（或别名 ``deepseek-legacy``）才走 DeepSeek。"""
    raw = (_env(environ).get("RESEARCH_LLM_PROVIDER") or "").strip().lower()
    if raw in _DEEPSEEK_VALUES:
        return PROVIDER_DEEPSEEK
    return PROVIDER_GROK


def provider_label(provider: str) -> str:
    """页面上、报错里的叫法。"""
    return _LABELS.get(provider, "LLM")


def llm_base_url(environ: Mapping[str, str] | None = None) -> str:
    """OpenAI 兼容的 base URL，不带 /chat/completions。"""
    env = _env(environ)
    return provider_base_url(llm_provider(env), env)


def provider_base_url(provider: str, environ: Mapping[str, str] | None = None) -> str:
    """指定那一家的 base URL。问库按次换提供方时用（researchlib.ask_model），不看 RESEARCH_LLM_PROVIDER。"""
    env = _env(environ)
    if provider == PROVIDER_GROK:
        return (env.get("XAI_BASE_URL", "").strip() or GROK_DEFAULT_BASE_URL).rstrip("/")
    return (env.get("DEEPSEEK_BASE_URL", "").strip() or DEEPSEEK_DEFAULT_BASE_URL).rstrip("/")


def deepseek_api_key(environ: Mapping[str, str] | None = None) -> str:
    return (_env(environ).get("DEEPSEEK_API_KEY") or "").strip()

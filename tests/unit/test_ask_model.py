"""问库按次选模型和思考强度：解析、默认、记下来的值、表单的兜底、换出的 Settings。不调用模型。"""

import json
import time
from dataclasses import replace

import pytest

from researchlib import ask_model, deepseek, grok, health
from researchlib import llm as llm_module
from researchlib.ask_model import (
    AskModel,
    apply,
    default_choice,
    form_choice,
    parse_choice,
    recorded_choice,
    stored_choice,
)
from researchlib.config import Settings
from researchlib.llm_provider import DEEPSEEK_DEFAULT_BASE_URL, GROK_DEFAULT_BASE_URL


def _settings(tmp_path, provider="grok") -> Settings:
    return Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused/research_test",
            "DATA_DIR": str(tmp_path),
            "RESEARCH_LLM_PROVIDER": provider,
            "XAI_OAUTH_TOKEN_PATH": str(tmp_path / "missing-oauth.json"),
        }
    )


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    for key in ("DEEPSEEK_API_KEY", "XAI_BASE_URL", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "XAI_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "missing-oauth.json"))


def test_default_is_the_site_provider_at_its_deepest_effort(tmp_path):
    # 问库保持最深思考：Grok 是 xhigh，DeepSeek 是 max。别的调用方（译制、识图）不走这里，仍是各家的全站档。
    assert default_choice(_settings(tmp_path)) == AskModel("grok", "xhigh")
    assert default_choice(_settings(tmp_path, "deepseek")) == AskModel("deepseek", "max")
    assert grok.GROK_REASONING_EFFORT == "high"
    assert set(ask_model.DEFAULT_EFFORT) == {"grok", "deepseek"}
    assert ask_model.DEFAULT_EFFORT["grok"] in grok.GROK_EFFORTS
    assert ask_model.DEFAULT_EFFORT["deepseek"] in deepseek.DEEPSEEK_EFFORTS


def test_parse_choice_fills_the_missing_half_and_rejects_unknown_values(tmp_path):
    settings = _settings(tmp_path)
    assert parse_choice(settings) == AskModel("grok", "xhigh")
    assert parse_choice(settings, "", " ") == AskModel("grok", "xhigh")
    assert parse_choice(settings, " DeepSeek ") == AskModel("deepseek", "max")
    assert parse_choice(settings, None, "LOW") == AskModel("grok", "low")
    assert parse_choice(settings, "deepseek", "high") == AskModel("deepseek", "high")
    with pytest.raises(ValueError, match="grok 或 deepseek"):
        parse_choice(settings, "gpt")
    # 档位按那一家认：DeepSeek 没有 xhigh，Grok 没有 max。
    with pytest.raises(ValueError, match="low、high、max"):
        parse_choice(settings, "deepseek", "xhigh")
    with pytest.raises(ValueError, match="low、medium、high、xhigh"):
        parse_choice(settings, "grok", "max")


def test_labels_are_what_the_menu_shows(tmp_path):
    assert AskModel("grok", "xhigh").label == "Grok · 最高"
    assert AskModel("deepseek", "max").label == "DeepSeek · 最高"
    assert AskModel("deepseek", "high").as_display() == {
        "provider": "deepseek",
        "effort": "high",
        "label": "DeepSeek · 高",
        "effort_label": "高",
    }


def test_recorded_choice_is_none_for_old_answers_and_stored_choice_falls_back(tmp_path):
    settings = _settings(tmp_path)
    for stats in (None, {}, {"llm": None}, {"llm": {}}, {"llm": {"provider": "gone", "effort": "x"}}, "x"):
        assert recorded_choice(settings, stats) is None
        assert stored_choice(settings, stats) == AskModel("grok", "xhigh")
    saved = {"llm": {"provider": "deepseek", "effort": "low"}, "progress": {"stage": "reading"}}
    assert recorded_choice(settings, saved) == AskModel("deepseek", "low")
    assert stored_choice(settings, saved) == AskModel("deepseek", "low")


def test_form_choice_never_fails_on_a_stale_effort(tmp_path):
    # 没脚本时换了模型、档位还是上一家的：用这一家的默认档；模型也不认就全按默认。
    settings = _settings(tmp_path)
    assert form_choice(settings, "deepseek", "xhigh") == AskModel("deepseek", "max")
    assert form_choice(settings, "grok", "medium") == AskModel("grok", "medium")
    assert form_choice(settings, "other", "max") == AskModel("grok", "xhigh")
    assert form_choice(settings, None, None) == AskModel("grok", "xhigh")


def test_apply_switches_provider_url_and_effort_without_touching_the_rest(tmp_path):
    settings = replace(_settings(tmp_path), llm_concurrency=5)
    chosen = apply(settings, AskModel("deepseek", "high"))
    assert (chosen.llm_provider, chosen.llm_base_url, chosen.llm_effort) == (
        "deepseek",
        DEEPSEEK_DEFAULT_BASE_URL,
        "high",
    )
    assert chosen.llm_concurrency == 5 and chosen.data_dir == settings.data_dir
    assert (settings.llm_provider, settings.llm_effort) == ("grok", "")
    back = apply(chosen, AskModel("grok", "low"))
    assert (back.llm_provider, back.llm_base_url, back.llm_effort) == ("grok", GROK_DEFAULT_BASE_URL, "low")


def test_menu_lists_only_providers_with_credentials_but_always_the_default(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    usable = {"grok"}
    monkeypatch.setattr(llm_module, "llm_configured", lambda s: s.llm_provider in usable)
    menu = ask_model.menu(settings)
    assert menu["default"] == {"provider": "grok", "effort": "xhigh"}
    assert menu["default_label"] == "Grok · 最高"
    assert [item["id"] for item in menu["providers"]] == ["grok"]
    grok_item = menu["providers"][0]
    assert grok_item["model"] == grok.GROK_MODEL_ID and grok_item["default_effort"] == "xhigh"
    assert [effort["id"] for effort in grok_item["efforts"]] == ["low", "medium", "high", "xhigh"]
    assert [effort["label"] for effort in grok_item["efforts"]] == ["低", "中", "高", "最高"]
    usable.add("deepseek")
    assert [item["id"] for item in ask_model.menu(settings)["providers"]] == ["grok", "deepseek"]
    # 默认那家没凭证也列着：选了照常报「没有可用的凭证」，不悄悄换另一家。
    usable.clear()
    assert [item["id"] for item in ask_model.menu(settings)["providers"]] == ["grok"]


def test_completion_params_take_the_chosen_effort_and_refuse_unknown_ones():
    assert grok.completion_params(temperature=0.2)["reasoning_effort"] == "high"
    assert grok.completion_params(temperature=0.2, effort="xhigh")["reasoning_effort"] == "xhigh"
    with pytest.raises(RuntimeError, match="Grok 没有这一档"):
        grok.completion_params(temperature=0.2, effort="max")
    assert deepseek.completion_params()["reasoning_effort"] == "max"
    assert deepseek.completion_params(effort="low")["reasoning_effort"] == "low"
    with pytest.raises(RuntimeError, match="DeepSeek 没有这一档"):
        deepseek.completion_params(effort="xhigh")


def _write_oauth(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": "oauth-access",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_at": time.time() * 1000 + 3_600_000,
                "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
                "needs_login": False,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )


def test_chat_sends_the_chosen_effort_only_when_settings_carry_one(tmp_path, monkeypatch):
    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    sent = []

    def fake_post(_settings, _bearer, payload):
        sent.append(payload["reasoning_effort"])
        return {"choices": [{"message": {"content": "好"}}], "model": "grok-4.7"}

    monkeypatch.setattr(llm_module, "_post", fake_post)
    health.reset_llm_cache()
    settings = _settings(tmp_path)
    llm_module.chat(settings, [{"role": "user", "content": "hi"}])
    llm_module.chat(apply(settings, AskModel("grok", "xhigh")), [{"role": "user", "content": "hi"}])
    assert sent == ["high", "xhigh"]
    health.reset_llm_cache()

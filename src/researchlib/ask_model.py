"""问库每次提问可以选模型和思考强度。

全站的提供方和强度由 ``RESEARCH_LLM_PROVIDER`` 和 ``researchlib.grok`` / ``researchlib.deepseek`` 定，
译制、识图、摘要、观点、论点照旧走那一组。问库（侧栏问库、文章页问这篇、JSON 接口）在输入框里多一个菜单：
选了哪家、哪一档，就用 ``apply`` 换出一份 ``Settings`` 往下传（``llm_provider``、``llm_base_url``、
``llm_effort``），扩词、分批读资料、写回答三处都用它。选择记在 ``llm_answers.stats.llm``，
「重新整合」沿用这一份，不会悄悄换回默认。

不选就是默认：全站那一家，强度用 ``DEFAULT_EFFORT``。没有凭证的那一家不列出来；
请求里点名了没凭证的那家，照常报「没有可用的 X 凭证」，不自动换另一家。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from researchlib.config import Settings
from researchlib.deepseek import DEEPSEEK_EFFORTS, deepseek_model
from researchlib.grok import GROK_EFFORTS, GROK_MODEL_ID
from researchlib.llm_provider import PROVIDER_DEEPSEEK, PROVIDER_GROK, provider_base_url, provider_label

EFFORTS = {PROVIDER_GROK: GROK_EFFORTS, PROVIDER_DEEPSEEK: DEEPSEEK_EFFORTS}
# 问库默认用各家的最高档（Zhilin 11-09 定问库保持最深思考，不为提速降强度）。
# 别的调用方不受影响：Grok 全站仍是 high，见 researchlib.grok。
DEFAULT_EFFORT = {PROVIDER_GROK: "xhigh", PROVIDER_DEEPSEEK: "max"}
EFFORT_LABELS = {"low": "低", "medium": "中", "high": "高", "xhigh": "最高", "max": "最高"}
PROVIDER_ORDER = (PROVIDER_GROK, PROVIDER_DEEPSEEK)


@dataclass(frozen=True)
class AskModel:
    provider: str
    effort: str

    @property
    def effort_label(self) -> str:
        return EFFORT_LABELS.get(self.effort, self.effort)

    @property
    def label(self) -> str:
        """菜单上显示的那几个字，比如「Grok · 最高」。"""
        return f"{provider_label(self.provider)} · {self.effort_label}"

    def as_json(self) -> dict:
        return {"provider": self.provider, "effort": self.effort}

    def as_display(self) -> dict:
        """接口里带出去的：记下的值，加上给人看的叫法。"""
        return {**self.as_json(), "label": self.label, "effort_label": self.effort_label}


def default_choice(settings: Settings) -> AskModel:
    provider = settings.llm_provider if settings.llm_provider in EFFORTS else PROVIDER_GROK
    return AskModel(provider, DEFAULT_EFFORT[provider])


def parse_choice(settings: Settings, model: object = None, effort: object = None) -> AskModel:
    """请求里的 model / effort。都空就是默认；只给一个时另一个取默认。不认识的值抛 ValueError。"""
    model_text = str(model or "").strip().lower()
    effort_text = str(effort or "").strip().lower()
    base = default_choice(settings)
    provider = model_text or base.provider
    if provider not in EFFORTS:
        raise ValueError(f"model 只能是 {' 或 '.join(PROVIDER_ORDER)}")
    allowed = EFFORTS[provider]
    chosen = effort_text or DEFAULT_EFFORT[provider]
    if chosen not in allowed:
        raise ValueError(f"{provider_label(provider)} 的 effort 只能是 {'、'.join(allowed)}")
    return AskModel(provider, chosen)


def recorded_choice(settings: Settings, stats: object) -> AskModel | None:
    """``llm_answers.stats.llm`` 里记的选择。以前的回答没记（或记的值现在不认了）回 None，页面上就不写档位。"""
    saved = stats.get("llm") if isinstance(stats, dict) else None
    if not isinstance(saved, dict) or not saved.get("provider"):
        return None
    try:
        return parse_choice(settings, saved.get("provider"), saved.get("effort"))
    except ValueError:
        return None


def stored_choice(settings: Settings, stats: object) -> AskModel:
    """重新整合用：记了就沿用，没记就按默认。"""
    return recorded_choice(settings, stats) or default_choice(settings)


def form_choice(settings: Settings, model: object, effort: object) -> AskModel:
    """没脚本时表单提交的选择：档位和模型对不上（换了模型、档位还是上一家的）就用这家的默认档，模型也不认就全按默认。"""
    for candidate in ((model, effort), (model, None)):
        try:
            return parse_choice(settings, *candidate)
        except ValueError:
            continue
    return default_choice(settings)


def apply(settings: Settings, choice: AskModel) -> Settings:
    """换成选的那家、那一档。llm.chat、llm_configured、missing_credential_text、active_model_id 都跟着它走。"""
    return replace(
        settings,
        llm_provider=choice.provider,
        llm_base_url=provider_base_url(choice.provider),
        llm_effort=choice.effort,
    )


def model_id(provider: str) -> str:
    if provider == PROVIDER_GROK:
        return GROK_MODEL_ID
    try:
        return deepseek_model()
    except RuntimeError:
        return "deepseek-flash"


def menu(settings: Settings) -> dict:
    """输入框里那个菜单要的东西：有凭证的几家、各家的档位、默认选哪个。"""
    from researchlib.llm import llm_configured

    default = default_choice(settings)
    providers = []
    for provider in PROVIDER_ORDER:
        candidate = apply(settings, AskModel(provider, DEFAULT_EFFORT[provider]))
        if provider != default.provider and not llm_configured(candidate):
            continue
        providers.append(
            {
                "id": provider,
                "label": provider_label(provider),
                "model": model_id(provider),
                "default_effort": DEFAULT_EFFORT[provider],
                "efforts": [{"id": item, "label": EFFORT_LABELS[item]} for item in EFFORTS[provider]],
            }
        )
    return {"default": default.as_json(), "default_label": default.label, "providers": providers}

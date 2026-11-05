"""显式选配的站内模型适配器：复用提供方/凭据/强度，不复用自动重试或预算扩大。"""
from __future__ import annotations

import copy
import json
import math
import urllib.request
from dataclasses import replace

from researchlib import llm
from researchlib.deepseek import completion_params as deepseek_params
from researchlib.experiments.schema import digest
from researchlib.grok import completion_params as grok_params
from researchlib.llm_provider import PROVIDER_GROK, PROVIDERS, provider_label

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不把原文和 Bearer 转发到提供方返回的其他地址。
        return None


class SiteModelGenerator:
    """构造/显示计划不读取凭据、不联网；只有界面确认后的 __call__ 才取凭据和调用。"""

    def __init__(self, settings):
        provider = settings.llm_provider
        if provider not in PROVIDERS:
            raise ValueError("实验模型仅支持站内 DeepSeek / Grok")
        if not math.isfinite(settings.llm_timeout) or settings.llm_timeout <= 0:
            raise ValueError("模型超时必须是有限的正数")
        self.settings = replace(settings, llm_timeout=min(settings.llm_timeout, 60.0))
        self.provider = provider
        self.payload = (grok_params(temperature=0.2) if provider == PROVIDER_GROK else deepseek_params())
        # 仅限本实验短回答；主站强度、下限和自动任务的请求都不改。
        self.payload = {**self.payload, "max_tokens": 1500, "stream": False}
        self.configuration_id = digest({
            "version": "site-model-once-v2", "provider": provider, "payload": self.payload,
            "base_url": settings.llm_base_url, "read_timeout_s": self.settings.llm_timeout,
        })
        self.label = provider_label(provider) + " · " + str(self.payload["model"])
        self.plan = {
            "id": self.configuration_id, "kind": "site_model", "label": self.label,
            "model": self.payload["model"], "max_output_tokens_per_side": 1500,
            "max_model_requests": 2, "automatic_retries": 0, "automatic_expansion": False,
            "read_timeout_s": self.settings.llm_timeout, "cost": None,
            "reasoning_effort": self.payload.get("reasoning_effort"),
        }

    def __call__(self, *, messages, max_output_tokens, before_send):
        if max_output_tokens != 1500 or sum(len(m.get("content", "")) for m in messages) > 40000:
            raise ValueError("实验输入或输出预算无效")
        if not callable(before_send):
            raise ValueError("站内实验模型缺少发送前授权检查")
        # 排队本身不授予发送权限。取得名额后先复查，避免已撤销的等待者读取/刷新凭据；
        # 凭据准备也可能阻塞，因此在所有准备结束后、紧邻 open 前再次检查。
        with llm._slot(self.settings):
            before_send()
            token = llm._oauth_token(self.settings) if self.provider == PROVIDER_GROK else llm._deepseek_token()
            request = urllib.request.Request(
                self.settings.llm_base_url + "/chat/completions",
                data=json.dumps({**copy.deepcopy(self.payload), "messages": messages}, ensure_ascii=False).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "Accept": "application/json"},
                method="POST",
            )
            opener = urllib.request.build_opener(_NoRedirect())
            before_send()
            with opener.open(request, timeout=self.settings.llm_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("模型响应超限，结果可能不完整且可能计费")
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("error"):
            raise ValueError("模型返回错误，费用可能未知")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or choices[0].get("finish_reason") != "stop":
            raise ValueError("模型回答没有完整结束，不自动重试")
        text = llm._message_content(body, self.label)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        def count(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None
        completed = count("completion_tokens")
        if completed is not None and completed > 1500:
            raise ValueError("提供方报告的输出用量超过请求预算，费用需核实")
        return {
            "text": text, "model": str(body.get("model") or self.payload["model"]),
            "model_version": "unknown", "input_tokens": count("prompt_tokens"), "output_tokens": completed,
            "cost": None, "currency": None, "request_profile": self.configuration_id,
        }

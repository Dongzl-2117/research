"""识图引擎交回结构、读不出来时怎么记、用量怎么报。不连库、不调真 DeepSeek。"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from researchlib import health
from researchlib import llm as llm_module
from researchlib.config import Settings
from researchlib.ocr import Recognition, describe_report, run_ocr, vision_engine

ROOT = Path(__file__).resolve().parents[2]
CHAT = {
    "kind": "chat",
    "blocks": [
        {"type": "message", "speaker": "王小明", "self": False, "text": "毛利率：35%，比去年高"},
        {"type": "message", "speaker": "", "self": True, "text": "先看现金流"},
    ],
}


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCH_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "missing-oauth.json"))
    monkeypatch.setattr(llm_module, "_STREAM_USAGE", True)
    health.reset_llm_cache()
    yield
    health.reset_llm_cache()


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
            "DISPLAY_TIMEZONE": "Asia/Shanghai",
            "DEEPSEEK_API_KEY": "sk-test-not-real",
        }
    )


def _image(tmp_path) -> Path:
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


def test_structured_reply_is_formatted_and_kept(tmp_path, monkeypatch):
    def fake_chat(_settings, _messages, **kwargs):
        kwargs["usage"].update({"prompt": 2100, "completion": 5400, "reasoning": 4800})
        return json.dumps(CHAT, ensure_ascii=False), "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    found = vision_engine(_settings(tmp_path))(_image(tmp_path))
    assert found.text == "王小明：毛利率：35%，比去年高\n\n方伟：先看现金流"
    assert found.units["kind"] == "chat"
    assert [block["self"] for block in found.units["blocks"]] == [False, True]
    assert found.usage == {"prompt": 2100, "completion": 5400, "reasoning": 4800}
    assert found.error == ""


def test_unreadable_structure_is_asked_once_more_then_given_up(tmp_path, monkeypatch):
    calls = []

    def fake_chat(_settings, _messages, **kwargs):
        calls.append(1)
        kwargs["usage"]["completion"] = kwargs["usage"].get("completion", 0) + 100
        return '{"kind": "chat", "blocks": [{"type": "message", "text": "说到一半', "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    found = vision_engine(_settings(tmp_path))(_image(tmp_path))
    assert len(calls) == 2
    assert found.text == "" and found.units is None
    assert "JSON" in found.error
    # 两次都花了钱，用量都记上。
    assert found.usage == {"completion": 200}


def test_second_try_can_succeed(tmp_path, monkeypatch):
    replies = ['{"kind": "chat", "blocks": [', json.dumps(CHAT, ensure_ascii=False)]

    monkeypatch.setattr("researchlib.llm.chat", lambda *_args, **_kwargs: (replies.pop(0), "deepseek-flash"))
    found = vision_engine(_settings(tmp_path))(_image(tmp_path))
    assert found.units is not None and found.error == ""


class _Conn:
    """记下写进 image_texts 的参数，别的查询都当没有结果。"""

    def __init__(self):
        self.writes: list[tuple] = []

    def execute(self, sql, params=None):
        if " ".join(sql.split()).startswith("INSERT INTO image_texts"):
            self.writes.append(params)

        class Result:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None

        return Result()

    def commit(self):
        return None


def _run(tmp_path, monkeypatch, recognize, count=1):
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    rows = []
    for index in range(count):
        (images / f"{index}.png").write_bytes(b"png")
        rows.append({"content_hash": f"{index:02d}" * 32, "storage_path": f"{index}.png"})
    monkeypatch.setattr("researchlib.ocr.pending_images", lambda *args, **kwargs: rows)
    conn = _Conn()
    return conn, run_ocr(conn, tmp_path, count, recognize, engine="deepseek-flash-vision")


def test_run_ocr_stores_units_usage_and_counts_formats(tmp_path, monkeypatch):
    results = [
        Recognition("王小明：毛利率：35%", {"v": 1, "kind": "chat", "blocks": []}, {"prompt": 10, "completion": 30}),
        Recognition("方伟：底线", None, {"prompt": 12, "completion": 20, "reasoning": 15}),
        Recognition("", None, {"completion": 40}, "识图回的 JSON 读不出来"),
        "旧引擎交回的字",
    ]
    conn, report = _run(tmp_path, monkeypatch, lambda _path: results.pop(0), count=4)
    engines = [write[1] for write in conn.writes]
    assert engines == [
        "deepseek-flash-vision",
        "deepseek-flash-vision",
        "deepseek-flash-vision:invalid",
        "deepseek-flash-vision",
    ]
    assert conn.writes[0][3].obj == {"v": 1, "kind": "chat", "blocks": []}
    assert conn.writes[1][3] is None
    assert conn.writes[2][2] == "" and conn.writes[2][3] is None
    assert conn.writes[2][4].obj == {"completion": 40}
    assert conn.writes[3][2] == "旧引擎交回的字" and conn.writes[3][4] is None
    assert (report["structured"], report["plain"], report["invalid"], report["failed"]) == (1, 2, 1, 0)
    assert report["tokens"] == {"prompt": 22, "completion": 90, "reasoning": 15}
    assert report["metered"] == 3
    lines = describe_report(report)
    assert "按结构交回 1 张" in lines[0] and "结构读不出来 1 张" in lines[0]
    assert "输入 22 token，输出 90 token（其中思考 15）" in lines[1]
    assert "平均每张输入 7、输出 30" in lines[1]


def test_report_says_when_the_api_reported_no_usage():
    report = {"structured": 2, "plain": 0, "invalid": 0, "tokens": {}, "metered": 0}
    assert describe_report(report)[-1] == "接口这次没报用量，花费看 DeepSeek 账单"
    assert describe_report({"structured": 0, "plain": 0, "invalid": 0}) == []


def test_chat_reports_usage_from_every_paid_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    payloads = []

    def fake_post(_settings, _token, payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return {
                "choices": [{"message": {"content": "", "reasoning_content": "想了很久"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 512,
                          "completion_tokens_details": {"reasoning_tokens": 512}},
            }
        return {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 900,
                      "completion_tokens_details": {"reasoning_tokens": 700}},
        }

    monkeypatch.setattr(llm_module, "_post", fake_post)
    usage: dict = {}
    settings = Settings.from_env({"DATABASE_URL": "postgresql://unused/research_test", "DATA_DIR": str(tmp_path)})
    text, _model = llm_module.chat(settings, [{"role": "user", "content": "hi"}], exact_max_tokens=512, usage=usage)
    assert text == "{}"
    assert usage == {"prompt": 200, "completion": 1412, "reasoning": 1212}
    assert all(payload["stream_options"] == {"include_usage": True} for payload in payloads)


def test_chat_without_usage_sends_the_same_payload_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    payloads = []

    def fake_post(_settings, _token, payload):
        payloads.append(dict(payload))
        return {"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 1}}

    monkeypatch.setattr(llm_module, "_post", fake_post)
    settings = Settings.from_env({"DATABASE_URL": "postgresql://unused/research_test", "DATA_DIR": str(tmp_path)})
    llm_module.chat(settings, [{"role": "user", "content": "hi"}])
    assert "stream_options" not in payloads[0]


@pytest.mark.parametrize(
    ("code", "message"),
    [(400, b'{"error": {"message": "unknown field: stream_options"}}'), (422, b'{"detail": "Unprocessable Entity"}')],
)
def test_chat_drops_stream_options_when_the_api_rejects_it(tmp_path, monkeypatch, code, message):
    """接口或转发不认这个字段时说法不一：4xx 都去掉它再发一次，识图照常，只是没有用量。

    去掉以后成了，说明就是这个字段的事：后面的请求不再带，免得每张图先白挨一次 4xx。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    payloads = []

    def fake_post(_settings, _token, payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            raise urllib.error.HTTPError("https://example.invalid", code, "Rejected", {}, io.BytesIO(message))
        return {"choices": [{"message": {"content": "好"}}]}

    monkeypatch.setattr(llm_module, "_post", fake_post)
    usage: dict = {}
    settings = Settings.from_env({"DATABASE_URL": "postgresql://unused/research_test", "DATA_DIR": str(tmp_path)})
    text, _model = llm_module.chat(settings, [{"role": "user", "content": "hi"}], usage=usage)
    assert text == "好"
    assert "stream_options" in payloads[0] and "stream_options" not in payloads[1]
    assert usage == {}
    llm_module.chat(settings, [{"role": "user", "content": "再来"}], usage=usage)
    assert len(payloads) == 3 and "stream_options" not in payloads[2]


def test_other_bad_request_is_resent_once_then_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    payloads = []

    def fake_post(_settings, _token, payload):
        payloads.append(dict(payload))
        body = io.BytesIO(b'{"error": {"message": "image too large"}}')
        raise urllib.error.HTTPError("https://example.invalid", 400, "Bad Request", {}, body)

    monkeypatch.setattr(llm_module, "_post", fake_post)
    settings = Settings.from_env({"DATABASE_URL": "postgresql://unused/research_test", "DATA_DIR": str(tmp_path)})
    with pytest.raises(llm_module.LLMError, match="image too large"):
        llm_module.chat(settings, [{"role": "user", "content": "hi"}], usage={})
    assert len(payloads) == 2
    # 不是这个字段的事：下一次照样要用量。
    assert llm_module._STREAM_USAGE is True

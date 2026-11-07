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
from researchlib.ocr import VISION_PROMPT_VERSION, Recognition, describe_report, run_ocr, vision_engine

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


def test_every_row_records_the_prompt_version(tmp_path, monkeypatch):
    """全库 --redo 靠它认出哪些图已经按现在的说明认过，停了再开不再付一次钱。失败的也记，但照样会重试。"""

    def flaky(path):
        if path.name == "1.png":
            raise RuntimeError("连不上")
        return Recognition("王小明：毛利率：35%", {"v": 1, "kind": "chat", "blocks": []})

    conn, report = _run(tmp_path, monkeypatch, flaky, count=2)
    assert [write[5] for write in conn.writes] == [VISION_PROMPT_VERSION, VISION_PROMPT_VERSION]
    assert conn.writes[1][1] == "deepseek-flash-vision:error" and report["failed"] == 1


def test_run_ocr_makes_way_for_a_waiting_deploy(tmp_path, monkeypatch):
    """自动上线在等（CI 过了的提交）：认完手上这张就停，以前全库重认会把之后的合并挡上好几天。"""
    status = tmp_path / "status"
    status.mkdir()
    (status / ".deploy-waiting").write_text("65ffa02abcdef\n", encoding="utf-8")
    calls = []

    def recognize(_path):
        calls.append(1)
        return Recognition("字")

    conn, report = _run(tmp_path, monkeypatch, recognize, count=3)
    assert calls == [] and conn.writes == []
    assert report["deploy_waiting"] == "65ffa02abcdef"
    # 点名几篇的定点重认很快就完，不让路。
    monkeypatch.setattr(
        "researchlib.ocr.pending_images",
        lambda *args, **kwargs: [{"content_hash": "00" * 32, "storage_path": "0.png"}],
    )
    pointed = run_ocr(_Conn(), tmp_path, 1, recognize, engine="deepseek-flash-vision", redo=True, article_ids=[7])
    assert calls == [1] and pointed["deploy_waiting"] == ""


def test_several_images_are_read_at_once_and_written_in_order(tmp_path, monkeypatch):
    """几张一起认：先认完的不先写，按挑出来的先后写（新文章的图在前）。"""
    import itertools
    import threading
    import time as time_module

    first_three = threading.Barrier(3)
    calls = itertools.count()

    def slow_first(path):
        if next(calls) < 3:
            first_three.wait(timeout=5)  # 三张同时在认才过得去，否则这张记失败
        if path.name == "0.png":
            time_module.sleep(0.1)
        return Recognition(f"字{path.stem}")

    images = tmp_path / "images"
    images.mkdir()
    rows = []
    for index in range(5):
        (images / f"{index}.png").write_bytes(b"png")
        rows.append({"content_hash": f"{index:02d}" * 32, "storage_path": f"{index}.png"})
    monkeypatch.setattr("researchlib.ocr.pending_images", lambda *args, **kwargs: rows)
    conn = _Conn()
    report = run_ocr(conn, tmp_path, 5, slow_first, engine="deepseek-flash-vision", workers=3)
    assert [write[2] for write in conn.writes] == ["字0", "字1", "字2", "字3", "字4"]
    assert report["images"] == 5 and report["failed"] == 0


def test_deadline_stops_new_images_but_writes_the_ones_already_paid_for(tmp_path, monkeypatch):
    """到点了不再开新的；已经发出去的几张照写，钱不白花。"""
    import threading
    from types import SimpleNamespace

    clock = {"now": 0.0}
    monkeypatch.setattr("researchlib.ocr.time", SimpleNamespace(monotonic=lambda: clock["now"]))
    both_sent = threading.Barrier(2)

    def recognize(path):
        both_sent.wait(timeout=5)  # 两张都发出去了
        clock["now"] = 100.0  # 这时过点
        return Recognition(f"字{path.stem}")

    images = tmp_path / "images"
    images.mkdir()
    rows = []
    for index in range(6):
        (images / f"{index}.png").write_bytes(b"png")
        rows.append({"content_hash": f"{index:02d}" * 32, "storage_path": f"{index}.png"})
    monkeypatch.setattr("researchlib.ocr.pending_images", lambda *args, **kwargs: rows)
    conn = _Conn()
    report = run_ocr(conn, tmp_path, 6, recognize, engine="deepseek-flash-vision", deadline=50.0, workers=2)
    assert report["images"] == 2 and [write[2] for write in conn.writes] == ["字0", "字1"]


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

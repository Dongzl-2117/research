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
    # 这个文件测的是识图 + chat 的传输细节（流式用量、stream_options 兼容），走明文 key 的那条分支；
    # 默认的 Grok OAuth 路径在 tests/unit/test_grok_strength.py 和 tests/unit/test_grok_migration.py 里测。
    monkeypatch.setenv("RESEARCH_LLM_PROVIDER", "deepseek-legacy")
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
    # #316 删了整理自由文字的旧规则，交回自由文字的按空行分段照原样排，日志别再写「按旧规则整理」。
    assert "交回自由文字 2 张（按空行分段，照原样排）" in lines[0]
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


def _rows(tmp_path, monkeypatch, count):
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    rows = []
    for index in range(count):
        (images / f"{index}.png").write_bytes(b"png")
        rows.append({"content_hash": f"{index:02d}" * 32, "storage_path": f"{index}.png"})
    monkeypatch.setattr("researchlib.ocr.pending_images", lambda *args, **kwargs: rows)
    return rows


def test_a_slow_image_does_not_hold_up_the_rest(tmp_path, monkeypatch):
    """几张一起认：认完一张写一张、马上补发。以前按挑出来的先后写，第一张慢就把后面都挡住。"""
    import threading

    others_done = threading.Event()
    finished: list[str] = []
    lock = threading.Lock()

    def recognize(path):
        if path.name == "0.png":
            # 只有两个位子：第一张还在认时，后面三张得一张接一张补发、认完，才等得到。
            assert others_done.wait(timeout=5), "后面的图被第一张挡住了"
        else:
            with lock:
                finished.append(path.name)
                if len(finished) == 3:
                    others_done.set()
        return Recognition(f"字{path.stem}")

    _rows(tmp_path, monkeypatch, 4)
    conn = _Conn()
    report = run_ocr(conn, tmp_path, 4, recognize, engine="deepseek-flash-vision", workers=2)
    written = [write[2] for write in conn.writes]
    # 第一张认完时第四张也刚认完，这两张谁先写都行；要紧的是前三张没等第一张。
    assert written[:2] == ["字1", "字2"] and sorted(written[2:]) == ["字0", "字3"]
    assert report["images"] == 4 and report["failed"] == 0


def test_running_out_of_balance_stops_the_whole_batch(tmp_path, monkeypatch):
    """余额不足（402）以后每张都会马上失败，接着认只会把剩下的图一张张记成失败。"""
    from researchlib.llm import LLMError

    calls = []

    def recognize(path):
        calls.append(path.name)
        raise LLMError("DeepSeek 接口返回 402：Insufficient Balance")

    _rows(tmp_path, monkeypatch, 3)
    conn = _Conn()
    with pytest.raises(LLMError, match="402"):
        run_ocr(conn, tmp_path, 3, recognize, engine="deepseek-flash-vision")
    assert calls == ["0.png"] and conn.writes == []


class _ConnWithText(_Conn):
    """image_texts 里这几张图已经有字。"""

    def __init__(self, with_text):
        super().__init__()
        self.with_text = set(with_text)

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT 1 FROM image_texts") and params and params[0] in self.with_text:

            class Hit:
                def fetchone(self_inner):
                    return {"?column?": 1}

                def fetchall(self_inner):
                    return [{"?column?": 1}]

            return Hit()
        return super().execute(sql, params)


def test_failed_redo_keeps_the_text_already_there(tmp_path, monkeypatch):
    """重认没成（失败、结构读不出来）不拿空正文盖掉库里的字，版本也不动，下次 --redo 接着认。"""
    outcomes = {
        "0.png": Recognition("", None, {"completion": 40}, "识图回的 JSON 读不出来"),  # 库里有字
        "1.png": RuntimeError("连不上"),  # 库里没字
        "2.png": Recognition("王小明：毛利率：35%", {"v": 1, "kind": "chat", "blocks": []}),  # 库里有字，认成了
        "3.png": RuntimeError("连不上"),  # 库里有字
    }

    def recognize(path):
        found = outcomes[path.name]
        if isinstance(found, Exception):
            raise found
        return found

    rows = _rows(tmp_path, monkeypatch, 4)
    had_text = [rows[0]["content_hash"], rows[2]["content_hash"], rows[3]["content_hash"]]
    conn = _ConnWithText(had_text)
    report = run_ocr(conn, tmp_path, 4, recognize, engine="deepseek-flash-vision", redo=True)
    assert [(write[0], write[1]) for write in conn.writes] == [
        (rows[1]["content_hash"], "deepseek-flash-vision:error"),
        (rows[2]["content_hash"], "deepseek-flash-vision"),
    ]
    assert (report["images"], report["kept"], report["failed"], report["invalid"]) == (4, 2, 2, 1)
    assert "重认没认成的 2 张留着旧字" in describe_report(report)[-1]

    # 不带 --redo 的照旧：失败记 :error、读不出来记 :invalid，下次按老规矩重试或点名重认。
    _rows(tmp_path, monkeypatch, 4)
    plain = _ConnWithText(had_text)
    report = run_ocr(plain, tmp_path, 4, recognize, engine="deepseek-flash-vision")
    assert [write[1] for write in plain.writes] == [
        "deepseek-flash-vision:invalid",
        "deepseek-flash-vision:error",
        "deepseek-flash-vision",
        "deepseek-flash-vision:error",
    ]
    assert report["kept"] == 0


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
    assert report["images"] == 2 and sorted(write[2] for write in conn.writes) == ["字0", "字1"]


def test_report_says_when_the_api_reported_no_usage():
    report = {"structured": 2, "plain": 0, "invalid": 0, "tokens": {}, "metered": 0}
    assert describe_report(report)[-1] == "接口这次没报用量，花费看提供方账单"
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
    settings = Settings.from_env({
        "DATABASE_URL": "postgresql://unused/research_test",
        "DATA_DIR": str(tmp_path),
        "RESEARCH_LLM_PROVIDER": "deepseek-legacy",
    })
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
    settings = Settings.from_env({
        "DATABASE_URL": "postgresql://unused/research_test",
        "DATA_DIR": str(tmp_path),
        "RESEARCH_LLM_PROVIDER": "deepseek-legacy",
    })
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
    settings = Settings.from_env({
        "DATABASE_URL": "postgresql://unused/research_test",
        "DATA_DIR": str(tmp_path),
        "RESEARCH_LLM_PROVIDER": "deepseek-legacy",
    })
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
    settings = Settings.from_env({
        "DATABASE_URL": "postgresql://unused/research_test",
        "DATA_DIR": str(tmp_path),
        "RESEARCH_LLM_PROVIDER": "deepseek-legacy",
    })
    with pytest.raises(llm_module.LLMError, match="image too large"):
        llm_module.chat(settings, [{"role": "user", "content": "hi"}], usage={})
    assert len(payloads) == 2
    # 不是这个字段的事：下一次照样要用量。
    assert llm_module._STREAM_USAGE is True

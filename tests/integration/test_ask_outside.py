"""外面的程序问库：MCP 的 ask_library 先回编号、get_answer 取结果；JSON 接口 wait=true 最多等一会儿；全站限次数。

模型用替身，不花钱。
"""

import os
import threading
import time

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import ask as ask_module
from researchlib.seed import load_sample
from researchlib.web.app import create_app

QUESTION = "宁德时代的毛利率怎么样"


def _sample(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)


def _rpc(client, msg_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return client.post("/api/mcp", json=message).json()


def _tool(client, msg_id, name, arguments):
    return _rpc(client, msg_id, "tools/call", {"name": name, "arguments": arguments})["result"]


def _asked(conn, n, *, status="done", hours_ago=1.0):
    """登记 n 次以前的问库（不跑）。"""
    for _ in range(n):
        conn.execute(
            "INSERT INTO llm_answers (question, scope, answer, citations, model, status, created_at)"
            " VALUES ('合成问题', '{}', '', '[]', '', %s, now() - make_interval(secs => %s))",
            (status, hours_ago * 3600),
        )
    conn.commit()


def _count(conn):
    return conn.execute("SELECT count(*) AS n FROM llm_answers").fetchone()["n"]


def _held_model(monkeypatch):
    """同义词马上回；写回答等到放行。返回放行用的 Event。"""
    release = threading.Event()

    def chat(_settings, messages, **_kwargs):
        if messages[0]["content"] == ask_module.EXPAND_PROMPT:
            return '{"同义": {}}', "synthetic-model"
        release.wait(10)
        return "宁德时代毛利率回升 [1]。", "synthetic-model"

    monkeypatch.setattr(ask_module, "chat", chat)
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    monkeypatch.setattr(ask_module, "_inline", lambda: False)
    return release


def _until_done(read):
    for _ in range(100):
        payload = read()
        if payload["status"] != "running":
            return payload
        time.sleep(0.1)
    return payload


def test_mcp_ask_returns_an_id_and_get_answer_reads_the_result(conn, settings, monkeypatch):
    _sample(conn, settings)
    release = _held_model(monkeypatch)
    with TestClient(create_app(settings)) as client:
        tools = {tool["name"]: tool for tool in _rpc(client, 1, "tools/list")["result"]["tools"]}
        # 问一次就登记一次、花一次钱：不标只读、不标可放心重复调。
        assert tools["ask_library"]["annotations"]["readOnlyHint"] is False
        assert tools["ask_library"]["annotations"]["idempotentHint"] is False
        assert tools["get_answer"]["annotations"]["readOnlyHint"] is True
        # 模型还没写完，工具就回了编号。
        started = _tool(client, 2, "ask_library", {"question": QUESTION})["structuredContent"]
        assert started["status"] == "running" and started["id"]
        assert "get_answer" in started["next"]
        running = _tool(client, 3, "get_answer", {"id": started["id"]})["structuredContent"]
        assert running["status"] == "running" and running["citations"] == []
        assert "get_answer" in running["next"]
        release.set()
        done = _until_done(lambda: _tool(client, 4, "get_answer", {"id": started["id"]})["structuredContent"])
        assert done["status"] == "done" and done["answer"] == "宁德时代毛利率回升 [1]。"
        # 只带回答里引用到的段落，每段带站内链接；没引用的不带。
        stored = conn.execute("SELECT citations FROM llm_answers WHERE id = %s", (started["id"],)).fetchone()
        assert len(stored["citations"]) > 1
        assert [item["n"] for item in done["citations"]] == [1]
        assert done["citations"][0]["links"]["html"].startswith("https://investment.zldong.com/articles/")
        assert done["citations"][0]["text"]
        assert "next" not in done and "answer_html" not in done
        for missing in (0, 10**30, started["id"] + 1):
            assert _tool(client, 5, "get_answer", {"id": missing})["isError"] is True
        assert client.get(f"/api/v1/answers/{10**30}").status_code == 404


def test_api_wait_gives_back_the_id_when_the_answer_takes_longer(conn, settings, monkeypatch):
    """wait=true 以前在请求里跑完整个整合，隔着 Cloudflare（100 秒）一定断开，回答编号也丢了。"""
    _sample(conn, settings)
    release = _held_model(monkeypatch)
    monkeypatch.setattr(ask_module, "WAIT_SECONDS", 0.5)
    with TestClient(create_app(settings)) as client:
        asked = client.post("/api/v1/ask", json={"question": QUESTION, "wait": True})
        assert asked.status_code == 200
        payload = asked.json()
        assert payload["status"] == "running" and payload["id"] and payload["checks"] == []
        release.set()
        done = _until_done(lambda: client.get(f"/api/v1/answers/{payload['id']}").json())
        assert done["status"] == "done" and done["answer"] == "宁德时代毛利率回升 [1]。"


def test_daily_limit_keeps_the_passages_and_says_when(conn, settings, monkeypatch):
    _sample(conn, settings)
    calls = []
    monkeypatch.setattr(ask_module, "chat", lambda *_args, **_kwargs: calls.append(1) or ("不该写", "x"))
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    monkeypatch.setattr(ask_module, "DAILY_LIMIT", 3)
    _asked(conn, 2, hours_ago=30)  # 一天以前的不算
    _asked(conn, 3, hours_ago=20)
    before = _count(conn)
    with TestClient(create_app(settings)) as client:
        page = client.post("/ask", data={"q": QUESTION})
        assert page.status_code == 200
        assert "最近 24 小时全站已经问了 3 次，到了每天 3 次的上限，约 4 小时后能再问" in page.text
        assert 'id="c-1"' in page.text
        api = client.post("/api/v1/ask", json={"question": QUESTION, "wait": True})
        assert api.status_code == 429
        assert api.json()["id"] is None and api.json()["citations"] and "上限" in api.json()["error"]
        tool = _tool(client, 1, "ask_library", {"question": QUESTION})
        assert tool["isError"] is True and "search_library" in tool["content"][0]["text"]
    assert not calls and _count(conn) == before


def test_running_limit_holds_new_questions_and_retries(conn, settings, monkeypatch):
    _sample(conn, settings)
    monkeypatch.setattr(ask_module, "chat", lambda _s, messages, **_k: ("重新整合好了 [1]。", "synthetic-model"))
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    monkeypatch.setattr(ask_module, "RUNNING_LIMIT", 2)
    failed = conn.execute(
        "INSERT INTO llm_answers (question, scope, answer, citations, model, status, error)"
        " VALUES (%s, %s, '', '[]', '', 'error', '服务重启，整合中断了。可以重新整合。') RETURNING id",
        (QUESTION, Jsonb({"source_ids": ["fangwei-mp", "fangwei-zsxq", "chengfengpolang"]})),
    ).fetchone()["id"]
    conn.commit()
    # 先起应用再登记在跑的：应用启动时会把上次没跑完的标成中断。
    with TestClient(create_app(settings)) as client:
        _asked(conn, 2, status="running", hours_ago=0)
        page = client.post("/ask", data={"q": QUESTION})
        assert "已经有 2 个问题在整合，等几分钟再问。先给出召回的原文段落。" in page.text
        assert client.post(f"/ask/{failed}/retry", follow_redirects=False).status_code == 303
        held = conn.execute("SELECT status, error FROM llm_answers WHERE id = %s", (failed,)).fetchone()
        assert held["status"] == "error" and "到时候再点「重新整合」" in held["error"]
        assert "到时候再点「重新整合」" in client.get(f"/ask/{failed}").text
        conn.execute("UPDATE llm_answers SET status = 'done' WHERE status = 'running'")
        conn.commit()
        client.post(f"/ask/{failed}/retry")
        assert "重新整合好了" in client.get(f"/ask/{failed}").text


def test_llms_txt_says_no_login_and_how_to_wait(conn, settings):
    _sample(conn, settings)
    with TestClient(create_app(settings)) as client:
        text = client.get("/llms.txt").text
    assert "Cloudflare Access" not in text and "不用登录" in text
    assert "get_answer" in text and "429" in text
    assert f"最近 24 小时最多 {ask_module.DAILY_LIMIT} 次" in text

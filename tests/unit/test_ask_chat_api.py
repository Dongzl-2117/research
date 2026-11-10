"""真实 HTTP 路由与范围解析，数据库和原问库执行器用替身；不调用模型。"""

from contextlib import nullcontext
from datetime import UTC, date, datetime
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from researchlib.ask_model import AskModel
from researchlib.snippets import answer_html

api_module = import_module("researchlib.web.api")


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        public_base_url="http://testserver", display_timezone="Etc/UTC", llm_provider="grok"
    )
    app.include_router(api_module.api)
    sources = [SimpleNamespace(id="synthetic", author="合成作者")]
    monkeypatch.setattr(api_module, "database", lambda _request: nullcontext(object()))
    monkeypatch.setattr(api_module, "active_sources", lambda *_args: sources)
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("wait", [False, True])
def test_api_preserves_every_scope_field_and_renders_with_original_executor(client, monkeypatch, wait):
    captured = []
    text = "## 结论\n\n渠道增长。[1000]\n\n```text\n[1000]\n```"
    evidence = [{"n": 1000, "url": "/articles/7?chunk=0", "cited": True}]

    def original(_conn, _settings, question, scope, sources, **kwargs):
        captured.append((question, scope, sources, kwargs))
        return {"id": 7, "status": "done", "answer": text, "citations": evidence}

    monkeypatch.setattr(api_module, "start", original)
    monkeypatch.setattr(api_module, "ask", original)
    response = client.post("/api/v1/ask", json={
        "question": "渠道增长", "source": "synthetic", "author": "合成作者", "company": "12",
        "from": "2026-10-01", "to": "2026-11-01", "article": "13", "tree": "14", "wait": wait,
        "history": [{"role": "assistant", "content": "旧回答不能成为证据"}],
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["answer"] == text
    assert payload["answer_html"] == str(answer_html(text, evidence))
    assert '<pre><code class="language-text">[1000]' in payload["answer_html"]
    question, scope, sources, kwargs = captured[0]
    assert question == "渠道增长" and len(captured) == 1
    # 没选模型：用全站那一家和问库的默认档。
    assert kwargs == {"choice": AskModel("grok", "xhigh")}
    assert scope.source_ids == ["synthetic"] and scope.company_id == 12
    assert scope.article_id == 13 and scope.tree_id == 14
    assert scope.date_from == date(2026, 10, 1) and scope.date_to == date(2026, 11, 1)
    assert not hasattr(scope, "history")


def test_poll_response_uses_same_safe_renderer_without_inventing_citations(client, monkeypatch):
    text = "[危险](javascript:alert(1))\n\n有效[1]，不存在[9]。"
    evidence = [{"n": 1, "url": "/articles/7?chunk=0", "cited": True}]
    now = datetime.now(UTC)
    row = {"id": 7, "question": "合成问题", "scope": {}, "status": "done", "error": "", "answer": text,
           "citations": evidence, "terms": [], "stats": {}, "model": "synthetic", "created_at": now, "updated_at": now}
    monkeypatch.setattr(api_module.queries, "get_answer", lambda *_args: row)
    monkeypatch.setattr(api_module, "cited_titles", lambda _conn, items, *_args: items)
    monkeypatch.setattr(api_module, "check_answer", lambda *_args: [])
    payload = client.get("/api/v1/answers/7").json()
    assert payload["answer"] == text and payload["citations"] == evidence
    assert payload["answer_html"] == str(answer_html(text, evidence))
    assert 'href="javascript:' not in payload["answer_html"]
    assert 'href="#c-1"' in payload["answer_html"] and 'href="#c-9"' not in payload["answer_html"]


def test_new_html_field_does_not_remove_origin_or_scope_guards(client, monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("拒绝的范围不能进入问库执行器")

    monkeypatch.setattr(api_module, "start", unexpected)
    assert client.post("/api/v1/ask", json={"question": "合成问题"},
                       headers={"Origin": "https://other.invalid"}).status_code == 403
    assert client.post("/api/v1/ask", json={"question": "合成问题", "source": "disabled"}).status_code == 404
    assert client.post("/api/v1/ask", json={"question": "合成问题", "chat": True}).status_code == 400


def test_api_passes_chosen_model_and_rejects_unknown_ones(client, monkeypatch):
    captured = []

    def original(_conn, _settings, _question, _scope, _sources, **kwargs):
        captured.append(kwargs["choice"])
        return {"id": 8, "status": "running", "answer": "", "citations": []}

    monkeypatch.setattr(api_module, "start", original)
    ok = client.post("/api/v1/ask", json={"question": "合成问题", "model": "DeepSeek", "effort": "high"})
    assert ok.status_code == 200 and captured == [AskModel("deepseek", "high")]
    # 只给模型：那一家的默认档。
    client.post("/api/v1/ask", json={"question": "合成问题", "model": "deepseek"})
    assert captured[-1] == AskModel("deepseek", "max")
    # 不认识的模型、这家没有的档位：400，说清能填什么，不进问库。
    unknown = client.post("/api/v1/ask", json={"question": "合成问题", "model": "other"})
    wrong = client.post("/api/v1/ask", json={"question": "合成问题", "model": "grok", "effort": "max"})
    assert unknown.status_code == wrong.status_code == 400
    assert "grok" in unknown.json()["detail"] and "xhigh" in wrong.json()["detail"]
    assert len(captured) == 2

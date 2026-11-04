"""阅读页保留侧栏界面，明确范围和独立提问；无脚本提交也走原问库。"""

import os

from fastapi.testclient import TestClient

from researchlib import ask as ask_module
from researchlib.seed import load_sample
from researchlib.web.app import create_app


def _sample(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)


def _article_id(conn, external_id):
    return conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()["id"]


def test_sidebar_exposes_scope_and_single_turn_and_noscript_uses_original_ask(conn, settings, monkeypatch):
    _sample(conn, settings)
    identity = _article_id(conn, "sample:battery-note")
    monkeypatch.setattr(ask_module, "llm_configured", lambda settings: False)
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{identity}").text
        assert 'id="article-chat"' in page and 'class="chat-toggle"' in page
        assert 'class="reader-grid"' in page and 'class="chat-chip"' in page
        assert 'name="article" aria-label="问库范围"' in page
        assert '<option value="">整个资料库</option>' in page
        assert f'<option value="{identity}">只问这篇</option>' in page
        assert '每轮独立提问，不沿用上文' in page
        assert 'form class="chat-form" action="/ask" method="post"' in page
        assert 'type="hidden" name="article"' not in page
        broad = client.post('/ask', data={'q': '腾讯', 'article': ''})
        local = client.post('/ask', data={'q': '概括这篇', 'article': str(identity)})
        assert broad.status_code == local.status_code == 200
        assert '只给出召回的原文段落' in broad.text
        assert '只给出召回的原文段落' in local.text
        assert '年初备忘' in local.text

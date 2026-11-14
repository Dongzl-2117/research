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
        assert 'class="reader-grid"' in page
        assert 'name="article" aria-label="问库范围"' in page
        assert '<option value="">整个资料库 · 单轮</option>' in page
        assert f'<option value="{identity}">只问这篇 · 单轮</option>' in page
        assert '每轮独立提问，不沿用上文' in page
        assert 'aria-describedby="article-chat-mode"' in page
        assert 'chat-chip' not in page and 'chat-hint' not in page
        assert 'placeholder=' not in page.split('id="article-chat-q"', 1)[1].split('>', 1)[0]
        assert 'form class="chat-form" action="/ask" method="post"' in page
        # 还没问时：问候和可以问的题；输入框里有模型和档位菜单（默认那家总在，档位按问库的默认选好）。
        panel = page.split('id="article-chat"', 1)[1].split('</aside>', 1)[0]
        assert '想从资料里了解什么？' in panel and f'data-scope="{identity}"' in panel
        assert '<details class="ask-model" data-popover' in panel and 'name="effort"' in panel
        assert 'value="xhigh" data-label="最高" checked' in panel
        assert 'type="hidden" name="article"' not in page
        broad = client.post('/ask', data={'q': '腾讯', 'article': ''})
        local = client.post('/ask', data={'q': '概括这篇', 'article': str(identity)})
        assert broad.status_code == local.status_code == 200
        assert '只给出找到的原文段落' in broad.text
        assert '只给出找到的原文段落' in local.text
        assert '年初备忘' in local.text

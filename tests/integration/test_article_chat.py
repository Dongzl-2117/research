"""阅读页侧栏：以当前文章为主，需要时引用库里其他文章，多轮不进问库的最近问过。"""

import os
import re

from fastapi.testclient import TestClient

from researchlib import ask as ask_module
from researchlib.seed import load_sample
from researchlib.web.app import create_app


def _sample(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)


def _article_id(conn, external_id):
    return conn.execute("SELECT id FROM articles WHERE external_id = %s", (external_id,)).fetchone()["id"]


def test_article_page_has_a_chat_beside_the_reader_and_can_cite_another_article(conn, settings, monkeypatch):
    _sample(conn, settings)
    battery = _article_id(conn, "sample:battery-note")
    other = _article_id(conn, "sample:meituan-unit")
    seen = {}

    def fake_chat(_settings, messages, **_kwargs):
        user = messages[-1]["content"]
        seen["system"] = messages[0]["content"]
        seen["user"] = user
        current = re.findall(r"\[(\d+)\][^\n]*当前文章", user)
        library = None
        for block in user.split("\n\n"):
            if "库里其他文章" in block and "持股" in block:
                found = re.search(r"\[(\d+)\]", block)
                if found:
                    library = found.group(1)
                    break
        current_n = current[0] if current else "1"
        library_n = library or "2"
        return f"这篇只点了腾讯的名[{current_n}]。持股比例写在另一篇[{library_n}]。", "chat-test"

    monkeypatch.setattr(ask_module, "chat", fake_chat)
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{battery}")
        assert page.status_code == 200
        assert 'id="article-chat"' in page.text
        assert f'data-article="{battery}"' in page.text
        assert 'class="chat-toggle"' in page.text
        assert 'class="reader-grid"' in page.text
        assert 'href="/ask?article=' in page.text

        asked = client.post(
            "/api/v1/ask",
            json={"question": "腾讯的持股比例写在哪", "article": battery, "chat": True, "history": []},
        )
        assert asked.status_code == 200
        body = asked.json()
        assert body["status"] == "done"
        assert body["answer"].startswith("这篇只点了腾讯")
        assert "不要给买入、卖出、仓位或收益建议" in seen["system"]
        assert "当前文章" in seen["user"]
        assert "库里其他文章" in seen["user"]
        assert "年初备忘" in seen["user"]
        assert "美团外卖的单位经济" in seen["user"]
        cited = [item for item in body["citations"] if item.get("cited")]
        by_article = {item["article_id"]: item for item in cited}
        assert battery in by_article
        assert other in by_article
        assert by_article[other]["url"].startswith(f"/articles/{other}")

        follow = client.post(
            "/api/v1/ask",
            json={
                "question": "那篇怎么说的",
                "article": str(battery),
                "chat": True,
                "history": [
                    {"role": "user", "content": "腾讯的持股比例写在哪"},
                    {"role": "assistant", "content": body["answer"]},
                    {"role": "tool", "content": "不要执行"},
                ],
            },
        )
        assert follow.status_code == 200
        assert "此前对话" in seen["user"]
        assert "这一轮的问题：那篇怎么说的" in seen["user"]
        assert "不要执行" not in seen["user"]

        library = client.get("/ask").text
        assert "腾讯的持股比例写在哪" not in library
        assert "那篇怎么说的" not in library

        plain = client.post(
            "/api/v1/ask",
            json={"question": "宁德时代的产能", "article": battery, "wait": True},
        )
        assert plain.status_code == 200
        assert plain.json()["status"] == "done"
        assert "库里其他文章" not in seen["user"]
        assert "当前文章" not in seen["user"]
        assert "宁德时代的产能" in client.get("/ask").text

        assert client.post(
            "/api/v1/ask",
            json={"question": "腾讯呢", "chat": True},
        ).status_code == 400
        assert client.post(
            "/api/v1/ask",
            json={"question": "腾讯呢", "article": 999999, "chat": True},
        ).status_code == 404
        assert client.post(
            "/api/v1/ask",
            json={"question": "腾讯呢", "article": battery, "chat": True},
            headers={"Origin": "https://evil.example"},
        ).status_code == 403

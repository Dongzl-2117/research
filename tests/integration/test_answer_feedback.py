"""回答页：引用核对、标「不相关」、补「漏了」，以及按标注回放检索。"""

import os

from fastapi.testclient import TestClient
from owner_fixtures import load_browsing_sample as load_sample

from researchlib import ask as ask_module
from researchlib.feedback import labelled_questions, parse_article_ref, replay, timing
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app


def _sample(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)


def _answer(conn, settings, monkeypatch, reply):
    monkeypatch.setattr(ask_module, "chat", lambda _s, messages, **_k: (reply, "grok-test"))
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '宁德时代'").fetchone()["id"]
    client = TestClient(create_app(settings))
    client.__enter__()
    posted = client.post("/ask", data={"q": "宁德时代的库存和产能", "company": str(company)}, follow_redirects=False)
    assert posted.status_code == 303
    return client, int(posted.headers["location"].rsplit("/", 1)[1])


def test_answer_page_lists_unverified_sentences(conn, settings, monkeypatch):
    _sample(conn, settings)
    reply = "宁德时代的产能利用率是 87%[1]。\n乘风破浪认为宁德时代库存在降[1]。"
    client, answer_id = _answer(conn, settings, monkeypatch, reply)
    try:
        page = client.get(f"/ask/{answer_id}").text
        assert "引用尚未核实" in page
        assert "数字 87% 不在所引段落里" in page
        assert "所引段落不是乘风破浪写的" in page
        data = client.get(f"/api/v1/answers/{answer_id}").json()
        assert data["checks"] and data["stats"]["passages"] >= 1 and "total_s" in data["stats"]
    finally:
        client.__exit__(None, None, None)


def test_marks_toggle_and_missing_articles_feed_the_replay(conn, settings, monkeypatch):
    _sample(conn, settings)
    client, answer_id = _answer(conn, settings, monkeypatch, "宁德时代的库存还没写完[1]。")
    try:
        row = conn.execute("SELECT citations FROM llm_answers WHERE id = %s", (answer_id,)).fetchone()
        first = row["citations"][0]
        form = {"label": "irrelevant", "article": first["article_id"], "chunk": first["ord"], "n": first["n"]}
        marked = client.post(f"/ask/{answer_id}/feedback", data=form, follow_redirects=False)
        assert marked.status_code == 303 and marked.headers["location"].endswith(f"#c-{first['n']}")
        assert "已标不相关" in client.get(f"/ask/{answer_id}").text
        client.post(f"/ask/{answer_id}/feedback", data=form)
        assert "已标不相关" not in client.get(f"/ask/{answer_id}").text
        client.post(f"/ask/{answer_id}/feedback", data=form)

        other = conn.execute("SELECT id, title FROM articles WHERE external_id = 'sample:pdd-note'").fetchone()
        added = client.post(
            f"/ask/{answer_id}/feedback",
            data={"label": "missing", "ref": f"https://investment.zldong.com/articles/{other['id']}?chunk=0"},
            follow_redirects=False,
        )
        assert added.status_code == 303
        page = client.get(f"/ask/{answer_id}").text
        assert other["title"] in page
        bad = client.post(f"/ask/{answer_id}/feedback", data={"label": "missing", "ref": "不是链接"})
        assert "贴文章链接或编号" in bad.text
    finally:
        client.__exit__(None, None, None)

    questions = labelled_questions(conn)
    assert len(questions) == 1 and questions[0]["missing"] == {other["id"]}
    sources = enabled_sources(load_sources(settings.sources_path))
    results = replay(conn, settings, sources)
    assert results[0]["missing"] == 1 and results[0]["irrelevant"] == 1
    # 漏的那篇（拼多多短帖）和宁德时代无关，回放时照样找不回来，报告里会写出来。
    assert results[0]["missing_still"] == [other["id"]]
    assert timing(conn)["answers"] >= 1


def test_article_refs():
    assert parse_article_ref("https://investment.zldong.com/articles/123?chunk=4") == (123, 4)
    assert parse_article_ref("/articles/9/zh") == (9, None)
    assert parse_article_ref(" 42 ") == (42, None)
    assert parse_article_ref("文章") == (None, None)

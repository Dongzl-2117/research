"""具体内容在全库出现过，范围内没有证据时也不能拿无关概览作答。"""

import re
from datetime import date
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from researchlib import ask as ask_module
from researchlib import retrieval as r
from researchlib.sources import enabled_sources, load_sources
from researchlib.web import api, mcp, routes
from researchlib.web.app import create_app
from researchlib.web.queries import Scope


def _corpus(conn):
    for source, identity, day, body in (
        ('fangwei-mp', 'known-outside', '2020-01-01', 'scopedneedle 只在较早的另一来源出现。' * 15),
        ('chengfengpolang', 'unrelated-inside', '2026-11-04', '另一篇文章只讨论渠道库存和门店经营。' * 15),
    ):
        row = conn.execute(
            'INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text,published_at)'
            ' VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (source, identity, identity, '合成笔记', body, body, day + 'T04:00:00+00:00'),
        ).fetchone()
        conn.execute('INSERT INTO article_chunks(article_id,ord,body) VALUES(%s,0,%s)', (row['id'], body))
    conn.commit()


@pytest.mark.parametrize('restriction', ['date', 'author', 'source_and_date'])
@pytest.mark.parametrize('purpose', ['display', 'llm', 'deep'])
def test_scoped_lexical_and_semantic_miss_returns_no_evidence(conn, settings, monkeypatch, restriction, purpose):
    _corpus(conn)
    sources = enabled_sources(load_sources(settings.sources_path))
    question = '乘风破浪 scopedneedle' if restriction == 'author' else 'scopedneedle'
    scope = Scope(
        source_ids=['chengfengpolang'] if restriction == 'source_and_date' else [s.id for s in sources],
        date_from=date(2026, 11, 4) if restriction != 'author' else None,
    )
    # 只替换向量服务；分析、词频、SQL 过滤、评分和问库均用真实实现。
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows, **_options: (rows, {}))
    assert any(t.text == 'scopedneedle' and t.df > 0 for t in r.analyze(conn, question, sources).content_terms)
    found = r.retrieve(conn, question, scope, sources, purpose=purpose)
    assert found.passages == [] and not found.query.overview
    assert found.query.content_terms

    def no_model(*args, **kwargs):
        pytest.fail('没有证据时不应调用模型')

    monkeypatch.setattr(ask_module, 'llm_configured', lambda _settings: True)
    monkeypatch.setattr(ask_module, 'chat', no_model)
    result = ask_module.ask(conn, settings, question, scope, sources)
    assert result['id'] is None and result['citations'] == []
    assert '范围内没有召回到任何段落' in result['error']
    assert conn.execute('SELECT count(*) AS n FROM llm_answers').fetchone()['n'] == 0


def test_explicit_day_overview_still_returns_that_days_article(conn, settings, monkeypatch):
    _corpus(conn)
    sources = enabled_sources(load_sources(settings.sources_path))
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows, **_options: (rows, {}))
    found = r.retrieve(
        conn, '这一天大家都在聊什么',
        Scope(source_ids=[s.id for s in sources], date_from=date(2026, 11, 4), date_to=date(2026, 11, 4)),
        sources, purpose='deep',
    )
    assert found.query.overview and found.passages
    assert all(p.source_id == 'chengfengpolang' for p in found.passages)
    assert all('scopedneedle' not in p.body for p in found.passages)


def test_invalid_filters_stop_search_and_ask_at_each_entrypoint(conn, settings, monkeypatch):
    _corpus(conn)

    def unexpected(*args, **kwargs):
        pytest.fail("无效的证据范围进入了问库登记或模型")

    for endpoint in (api, routes, mcp):
        monkeypatch.setattr(endpoint, "start", unexpected)
    with TestClient(create_app(settings)) as client:
        for field, value in (("from", "2026-02-30"), ("to", "2026-01-01错"),
                             ("article", "bad"), ("tree", 2**63), ("article", 0)):
            options = {field: value, "source": "fangwei-mp"}
            for path in ("/api/v1/articles", "/api/v1/search", "/search", "/ask"):
                response = client.get(path, params=options)
                assert response.status_code == 400, (path, options, response.status_code)
                assert field in response.text
            assert client.post("/ask", data={"q": "scopedneedle", **options}).status_code == 400
            assert client.post("/api/v1/ask", json={"question": "scopedneedle", **options}).status_code == 400
            for name, prompt in (("search_library", "q"), ("ask_library", "question")):
                result = client.post("/api/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": {prompt: "scopedneedle", **options}},
                }).json()["result"]
                assert result["isError"] and field in result["content"][0]["text"]
        assert client.get("/", params={"date": "bad"}).status_code == 200
        assert client.get("/api/v1/articles", params={"from": "", "tree": ""}).json()["total"] == 2
        result = client.get("/api/v1/articles", params={
            "from": "2026-11-05T12:34:56+08:00", "to": "2026-11-04",
        }).json()
        assert (result["scope"]["from"], result["scope"]["to"], result["total"]) == ("2026-11-04", "2026-11-05", 1)
        assert client.get("/api/v1/articles", params={"article": 999999}).json()["total"] == 0


def test_tree_scope_survives_search_navigation_and_mcp(conn, settings):
    _corpus(conn)
    rows = conn.execute("SELECT id FROM articles ORDER BY id").fetchall()
    wanted, outside = (row["id"] for row in rows)
    with TestClient(create_app(settings)) as client:
        for view in ("passage", "article"):
            params = {"q": "scopedneedle", "tree": wanted, "view": view, "from": "2019-01-01"}
            page = client.get("/search", params=params)
            assert page.status_code == 200
            assert f'name="tree" value="{wanted}"' in page.text
            ask_url = re.search(r'<a class="ask-card" href="([^"]+)"', page.text).group(1)
            clear_url = re.search(r'<a href="([^"]+)">清除</a>', page.text).group(1)
            ask_params = parse_qs(urlsplit(unescape(ask_url)).query)
            clear_params = parse_qs(urlsplit(unescape(clear_url)).query)
            assert ask_params["tree"] == clear_params["tree"] == [str(wanted)]
            assert ask_params["from"] == ["2019-01-01"] and "from" not in clear_params
        for tree, expected in ((wanted, [wanted]), (outside, [])):
            arguments = {"q": "scopedneedle", "level": "article", "tree": tree}
            result = client.post("/api/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_library", "arguments": arguments},
            }).json()["result"]["structuredContent"]
            assert result["scope"]["tree"] == tree
            assert [article["id"] for article in result["items"]] == expected

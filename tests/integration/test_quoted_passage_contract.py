"""逐段召回和ASK必须满足每个显式引号组；文章检索仍允许跨块匹配。"""

from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient

from researchlib import ask as ask_module
from researchlib import retrieval as r
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope


@pytest.fixture
def quoted(conn, settings, monkeypatch):
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows: (rows, {}))
    monkeypatch.setattr(ask_module, 'llm_configured', lambda settings: False)
    sources = enabled_sources(load_sources(settings.sources_path))
    body = 'search alpha beta pricing 定价权。 ' + 'Synthetic supporting context. ' * 20
    identity = conn.execute(
        "INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text,published_at)"
        " VALUES('fangwei-mp','synthetic-quotes','synthetic-quotes','合成引号',%s,%s,'2026-01-01') RETURNING id",
        (body, body),
    ).fetchone()['id']
    for index, text in enumerate(['alpha only', 'beta only', 'search alpha beta 定价权', 'alpha forbidden phrase']):
        conn.execute('INSERT INTO article_chunks(article_id,ord,body) VALUES(%s,%s,%s)', (identity, index, text))
    conn.commit()
    return sources, Scope(source_ids=['fangwei-mp']), identity


@pytest.mark.parametrize('question,phrase', [('search"alpha beta"', 'alpha beta'), ('搜索“定价权”', '定价权')])
def test_adjacent_quotes_remain_a_phrase_in_api_and_ask(conn, settings, quoted, question, phrase):
    sources, scope, _ = quoted
    analyzed = r.analyze(conn, question, sources)
    assert any(t.kind == 'phrase' and t.text == phrase for t in analyzed.terms)
    with TestClient(create_app(settings)) as client:
        payload = client.get('/api/v1/search', params={'q': question, 'level': 'chunk'}).json()
    assert payload['passages'] and all(phrase in p['text'] for p in payload['passages'])
    found = ask_module.start(conn, settings, question, scope, sources, run_inline=True)
    assert found['citations'] and all(phrase in p['text'] for p in found['citations'])


def test_and_phrases_are_required_per_passage_for_api_and_ask(conn, settings, quoted, monkeypatch):
    sources, scope, _ = quoted
    question = '"alpha" "beta"'
    for purpose in ('display', 'llm', 'deep'):
        found = r.retrieve(conn, question, scope, sources, purpose=purpose)
        assert found.passages and all('alpha' in p.body and 'beta' in p.body for p in found.passages)
    with TestClient(create_app(settings)) as client:
        payload = client.get('/api/v1/search', params={'q': question, 'level': 'chunk'}).json()
    assert all('alpha' in p['text'] and 'beta' in p['text'] for p in payload['passages'])
    result = ask_module.start(conn, settings, question, scope, sources, run_inline=True)
    assert all('alpha' in p['text'] and 'beta' in p['text'] for p in result['citations'])
    monkeypatch.setattr(ask_module, 'llm_configured', lambda settings: True)
    monkeypatch.setattr(ask_module, 'chat', lambda *a, **kw: ('合成回答 [1]。', 'synthetic'))
    saved = ask_module.ask(conn, settings, question, scope, sources)
    assert saved['status'] == 'done' and saved['citations']
    assert all(p['ord'] == 2 for p in saved['citations'])


def test_quoted_or_negative_and_article_cross_chunk_controls(conn, settings, quoted):
    sources, scope, identity = quoted
    found = r.retrieve(conn, '"alpha"OR"beta" -"forbidden phrase"', scope, sources, purpose='display')
    assert any(p.ord == 1 for p in found.passages)  # 只有 beta 也可匹配 OR；每篇仍受正常段数预算限制。
    assert {p.ord for p in found.passages} <= {0, 1, 2}
    conn.execute('DELETE FROM article_chunks WHERE article_id=%s AND ord>=2', (identity,))
    conn.commit()
    r.reset_stats_cache()
    assert r.retrieve(conn, '"alpha" "beta"', scope, sources, purpose='display').passages == []
    result = ask_module.start(conn, settings, '"alpha" "beta"', scope, sources, run_inline=True)
    assert result['id'] is None and result['citations'] == []
    narrowed = replace(scope, date_from=date(2026, 1, 1))
    assert r.retrieve(conn, '"alpha" "beta"', narrowed, sources, purpose='display').passages == []
    # 文章列表的两条证据可分属同篇不同块，不能因此漏掉完整文章。
    article = r.search_articles(conn, '"alpha" "beta"', scope, sources)
    assert article.total == 1 and article.hits[0].article_id == identity


def test_expansion_cannot_make_a_mandatory_quote_optional(conn, settings, quoted):
    sources, scope, _ = quoted
    found = r.retrieve(conn, '"alpha" "beta"', scope, sources, purpose='display', expansions={'alpha': ['only']})
    assert found.passages and all('alpha' in p.body and 'beta' in p.body for p in found.passages)

"""标题证据不在正文块词频里，不能在零词频分支被清空。"""

import pytest
from fastapi.testclient import TestClient

from researchlib import retrieval as r
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope


@pytest.mark.parametrize('question', ['uniquetitle', 'UNIQUETITLE'])
def test_exact_title_only_lexical_evidence_survives_without_vectors(conn, settings, monkeypatch, question):
    body = 'Generic supporting context about independent distribution mechanisms. ' * 15
    identity = conn.execute(
        "INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text)"
        " VALUES('fangwei-mp','synthetic-title','synthetic-title','uniquetitle',%s,%s) RETURNING id",
        (body, body),
    ).fetchone()['id']
    conn.execute('INSERT INTO article_chunks(article_id,ord,body) VALUES(%s,0,%s)', (identity, body))
    conn.commit()
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows: (rows, {}))
    sources = enabled_sources(load_sources(settings.sources_path))
    scope = Scope(source_ids=['fangwei-mp'])
    assert all(term.df == 0 for term in r.analyze(conn, question, sources).content_terms)
    found = r.retrieve(conn, question, scope, sources, purpose='display')
    assert [p.article_id for p in found.passages] == [identity]
    with TestClient(create_app(settings)) as client:
        payload = client.get('/api/v1/search', params={'q': question, 'level': 'chunk'}).json()
    assert [p['article_id'] for p in payload['passages']] == [identity]
    # 仍无标题／正文证据的问题不可退成这个文章的概览。
    assert r.retrieve(conn, 'absenttitle', scope, sources, purpose='display').passages == []

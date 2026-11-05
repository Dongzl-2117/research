"""具体内容在全库出现过，范围内没有证据时也不能拿无关概览作答。"""

from datetime import date

import pytest

from researchlib import ask as ask_module
from researchlib import retrieval as r
from researchlib.sources import enabled_sources, load_sources
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
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows: (rows, {}))
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
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows: (rows, {}))
    found = r.retrieve(
        conn, '这一天大家都在聊什么',
        Scope(source_ids=[s.id for s in sources], date_from=date(2026, 11, 4), date_to=date(2026, 11, 4)),
        sources, purpose='deep',
    )
    assert found.query.overview and found.passages
    assert all(p.source_id == 'chengfengpolang' for p in found.passages)
    assert all('scopedneedle' not in p.body for p in found.passages)

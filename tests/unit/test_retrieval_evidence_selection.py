"""命中证据不能因同篇译文消失；零词频问题不能改成无关概览。"""

from datetime import date
from types import SimpleNamespace

import pytest

from researchlib import embeddings
from researchlib import retrieval as r
from researchlib.chunks import TRANSLATION_ORD_BASE
from researchlib.web.queries import Scope


def query(**kwargs):
    fields = dict(raw='synthetic unseen concept', terms=[], companies=[], authors=[], excludes=[], pairs=[], years=[],
                  recent=False, overview=False, timeline=False, author_intent=False)
    return r.Query(**{**fields, **kwargs})


class Conn:
    def execute(self, *a, **kw):
        return self

    def fetchall(self):
        return []


def test_translation_does_not_discard_original_or_ocr_evidence():
    rows = [{'article_id': 1, 'ord': 0, 'lang': 'en', 'body': 'unique original evidence'},
            {'article_id': 1, 'ord': 100000, 'lang': 'ocr', 'body': 'unique chart evidence'},
            {'article_id': 1, 'ord': TRANSLATION_ORD_BASE, 'lang': 'zh', 'body': '译文概览'}]
    assert r._prefer_translation(rows) == rows


def test_overview_position_is_relative_to_each_language(monkeypatch):
    monkeypatch.setattr(r, '_corpus_stats', lambda _conn: (2, 200))
    base = dict(article_id=1, heading='', body='同样的合成正文。', title='合成', source_id='synthetic',
                published_at=None)
    passages = r.score_rows(Conn(), query(overview=True), [dict(base, ord=0, lang='en'),
                         dict(base, ord=TRANSLATION_ORD_BASE, lang='zh')], scope=Scope(source_ids=['synthetic']))
    assert len(passages) == 2 and passages[0].score == passages[1].score


def test_zero_df_keeps_the_content_question_and_invokes_semantic_recall(monkeypatch):
    term = r.Term('unseen', 'word', 0, forms=['unseen'], df=0, idf=1)
    q = query(terms=[term])
    monkeypatch.setattr(r, 'load_company_rows', lambda c: [])
    monkeypatch.setattr(r, 'analyze', lambda *a, **k: q)
    monkeypatch.setattr(r, '_candidates', lambda *a, **k: [])
    called = []
    row = dict(article_id=1, ord=0, body='合成语义证据', heading='', title='合成', source_id='synthetic',
               published_at=None)

    def semantic(conn, got, scope, rows):
        called.append(got)
        assert not got.overview and got.terms
        return [row], {(1, 0): 1.0}

    monkeypatch.setattr(r, 'semantic_candidates', semantic)
    monkeypatch.setattr(r, '_corpus_stats', lambda c: (1, 100))
    found = r.retrieve(Conn(), 'unseen', Scope(source_ids=['synthetic']), [SimpleNamespace(id='synthetic')])
    assert called and found.passages and found.passages[0].body == '合成语义证据'


def test_zero_df_without_vectors_returns_no_unrelated_overview(monkeypatch):
    term = r.Term('unseen', 'word', 0, forms=['unseen'], df=0, idf=1)
    monkeypatch.setattr(r, 'load_company_rows', lambda c: [])
    monkeypatch.setattr(r, 'analyze', lambda *a, **k: query(terms=[term]))
    row = dict(article_id=1, ord=0, body='无关概览', heading='', title='合成', source_id='synthetic', published_at=None)
    monkeypatch.setattr(r, '_candidates', lambda c, got, *a, **k: [row] if got.overview else [])
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows: (rows, {}))
    monkeypatch.setattr(r, '_corpus_stats', lambda c: (1, 100))
    found = r.retrieve(Conn(), 'unseen', Scope(source_ids=['synthetic'], company_id=1),
                       [SimpleNamespace(id='synthetic')])
    assert found.passages == [] and not found.query.overview


@pytest.mark.parametrize('restriction', ['date', 'company', 'author', 'source'])
def test_globally_known_content_without_scoped_evidence_stays_empty(monkeypatch, restriction):
    """词在别处出现过，不等于所选范围里有证据。"""
    term = r.Term('known', 'word', 0, forms=['known'], df=3, idf=1)
    q = query(terms=[term], authors=['合成作者'] if restriction == 'author' else [])
    scopes = {
        'date': Scope(date_from=date(2026, 11, 4)),
        'company': Scope(company_id=1),
        'author': Scope(),
        'source': Scope(source_ids=['synthetic']),
    }
    row = dict(article_id=1, ord=0, body='无关概览', heading='', title='合成', source_id='synthetic',
               published_at=None)
    monkeypatch.setattr(r, 'load_company_rows', lambda c: [])
    monkeypatch.setattr(r, 'analyze', lambda *a, **k: q)
    monkeypatch.setattr(r, '_candidates', lambda c, got, *a, **k: [row] if got.overview else [])
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows: (rows, {}))
    monkeypatch.setattr(r, '_corpus_stats', lambda c: (1, 100))
    found = r.retrieve(Conn(), 'known', scopes[restriction], [SimpleNamespace(id='synthetic')], purpose='display')
    assert found.passages == []
    assert not found.query.overview and found.query.content_terms == [term]


@pytest.mark.parametrize('explicit_day', [False, True])
def test_intentional_overview_still_returns_scoped_material(monkeypatch, explicit_day):
    q = query(raw='这一天大家都在聊什么' if explicit_day else '概览', overview=not explicit_day)
    row = dict(article_id=1, ord=0, body='所选日期的合成概览。', heading='', title='合成', source_id='synthetic',
               published_at=None)
    monkeypatch.setattr(r, 'load_company_rows', lambda c: [])
    monkeypatch.setattr(r, 'analyze', lambda *a, **k: q)
    monkeypatch.setattr(r, '_candidates', lambda c, got, *a, **k: [row] if got.overview else [])
    monkeypatch.setattr(r, '_corpus_stats', lambda c: (1, 100))
    found = r.retrieve(Conn(), q.raw, Scope(date_from=date(2026, 11, 4)),
                       [SimpleNamespace(id='synthetic')], purpose='display')
    assert found.query.overview
    assert [p.body for p in found.passages] == [row['body']]


def test_embedding_input_preserves_empty_field_slots_in_its_hash_contract():
    row = {'title': '合成', 'heading': '', 'body': '正文'}
    assert embeddings._source_text(row) == '合成\n\n正文'


@pytest.mark.parametrize('old_fails', [False, True])
def test_inflight_query_cannot_cache_or_poison_a_different_model(old_fails):
    old_vector, new_vector = [1.0], [2.0]

    def changed(texts):
        embeddings.use_embedder(lambda values: [new_vector for _ in values], model='synthetic-new')
        if old_fails:
            raise embeddings.EmbeddingError('旧模型的合成错误')
        return [old_vector for _ in texts]

    embeddings.use_embedder(changed, model='synthetic-old')
    try:
        assert embeddings.query_vector('合成问题') is None
        assert embeddings.query_vector('合成问题') == new_vector
    finally:
        embeddings.use_embedder(None)

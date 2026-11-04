"""引号、作者、消歧与文章集合计数必须一致；仅隔离合成资料。"""

import re

import pytest
from fastapi.testclient import TestClient

from researchlib import retrieval as r
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope


@pytest.fixture
def search_state(conn, settings, monkeypatch):
    sources = enabled_sources(load_sources(settings.sources_path))
    monkeypatch.setattr(r, 'semantic_candidates', lambda c, q, s, rows: (rows, {}))
    scope = Scope(source_ids=[source.id for source in sources])

    def add(name, body, *, title=None, source='fangwei-mp', ocr=None):
        row = conn.execute(
            'INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text)'
            ' VALUES(%s,%s,%s,%s,%s,%s) RETURNING id', (source, name, name, title or name, body, body),
        ).fetchone()
        identity = row['id']
        conn.execute('INSERT INTO article_chunks(article_id,ord,body) VALUES(%s,0,%s)', (identity, body))
        if ocr:
            conn.execute("INSERT INTO article_chunks(article_id,ord,body,lang) VALUES(%s,20000,%s,'ocr')",
                         (identity, ocr))
        conn.commit()
        r.reset_stats_cache()
        return identity

    def search(text, **kwargs):
        return r.search_articles(conn, text, kwargs.pop('scope', scope), sources, **kwargs)

    return add, search, sources


def test_quoted_or_and_negative_phrase_keep_their_boolean_meaning(conn, search_state):
    add, search, _sources = search_state
    first = add('合成甲', 'alphaneedle phrase explains the mechanism.')
    second = add('合成乙', 'betaneedle phrase explains the mechanism.')
    excluded = add('合成丙', 'alphaneedle phrase explains the mechanism. bad phrase appears here.')
    punctuated = add('合成丁', 'alphaneedle, phrase is separated by punctuation.')
    found = search('"alphaneedle phrase" OR "betaneedle phrase" -"bad phrase"', limit=30)
    assert found.total == found.available == sum(found.counts.values()) == 2
    assert {hit.article_id for hit in found.hits} == {first, second}
    assert excluded not in {hit.article_id for hit in found.hits}
    assert punctuated not in {hit.article_id for hit in found.hits}
    assert found.query.excludes == ['bad phrase']
    assert len({term.group for term in found.query.terms}) == 1


def test_phrase_or_unquoted_word_and_cross_chunk_and_groups(conn, search_state):
    add, search, _ = search_state
    first = add('合成甲', 'alphaneedle phrase redneedle')
    second = add('合成乙', 'otherneedle redneedle')
    add('合成丙', 'alphaneedle, phrase redneedle')
    found = search('"alphaneedle phrase" OR otherneedle redneedle', limit=30)
    assert found.total == 2
    assert {hit.article_id for hit in found.hits if hit.full} == {first, second}


def test_multiword_author_and_explicit_source_are_an_intersection(conn, search_state):
    add, search, sources = search_state
    target = add('合成英文作者', 'scopeevidence discusses independent mechanisms.', source='stratechery')
    add('合成其它作者', 'scopeevidence discusses another mechanism.')
    assert search('Ben Thompson').query.authors == ['Ben Thompson']
    assert [hit.article_id for hit in search('Ben Thompson').hits] == [target]
    constrained = Scope(source_ids=['fangwei-mp'])
    assert search('Ben Thompson scopeevidence', scope=constrained).hits == []
    found = r.retrieve(conn, 'Ben Thompson scopeevidence', constrained, sources, purpose='search')
    assert found.passages == [] and found.scope.source_ids == []


def test_title_alias_cannot_reintroduce_a_disambiguated_company(conn, search_state):
    add, search, _ = search_state
    fruit = add('合成果园', '果园的苹果库存上升，水果批发渠道改变。', title='苹果库存研究')
    tech = add('合成设备', '苹果的 iPhone 库存管理改善。', title='苹果库存研究')
    company = conn.execute("SELECT id FROM companies WHERE name_zh='苹果'").fetchone()['id']
    conn.execute("INSERT INTO article_mentions VALUES(%s,%s,'title',2)", (tech, company))
    conn.commit()
    found = search('苹果 库存', limit=30)
    assert found.total == sum(found.counts.values()) == 1
    assert {hit.article_id for hit in found.hits if hit.full} == {tech}
    assert not any(hit.article_id == fruit and hit.full for hit in found.hits)


def test_counts_are_the_union_of_index_and_cross_original_ocr_evidence(conn, search_state, monkeypatch):
    add, search, _ = search_state
    indexed = add('合成字面', 'redneedle blueneedle explain the mechanism.')
    cross = add('合成跨图', 'redneedle explains the mechanism.', ocr='blueneedle is in the chart.')
    partial = add('合成部分', 'redneedle explains a different topic.')
    rows = r._rows_for_keys(conn, [(cross, 0), (cross, 20000), (partial, 0)])
    monkeypatch.setattr(r, '_candidates', lambda *a, **kw: rows)
    found = search('redneedle blueneedle', limit=30)
    assert found.total == sum(found.counts.values()) == 2
    assert found.available == 3 and found.partial == 1 and found.semantic == 0
    assert {hit.article_id for hit in found.hits if hit.full} == {indexed, cross}
    pages = [hit.article_id for offset in range(3)
             for hit in search('redneedle blueneedle', limit=1, offset=offset).hits]
    assert len(pages) == len(set(pages)) == 3


def test_exclusion_applies_to_matching_evidence_in_both_list_and_counts(conn, search_state):
    add, search, _ = search_state
    keep = add('合成保留', 'redneedle explains the mechanism.')
    elsewhere = add('合成别处', 'redneedle explains the mechanism.', ocr='bad phrase belongs to another image.')
    add('合成排除', 'redneedle bad phrase explains the mechanism.')
    found = search('redneedle -"bad phrase"')
    assert found.total == found.available == sum(found.counts.values()) == 2
    assert {hit.article_id for hit in found.hits} == {keep, elsewhere}


def test_company_exclusion_cannot_count_articles_with_no_eligible_evidence(conn, search_state):
    add, search, _ = search_state
    excluded = add('合成设备', '苹果的 iPhone 业务解释经营机制。')
    company = conn.execute("SELECT id FROM companies WHERE name_zh='苹果'").fetchone()['id']
    conn.execute("INSERT INTO article_mentions VALUES(%s,%s,'whole',2)", (excluded, company))
    conn.commit()
    found = search('苹果 -iPhone')
    assert found.total == found.available == sum(found.counts.values()) == 0
    assert found.hits == []
    conn.execute('INSERT INTO article_chunks(article_id,ord,body) VALUES(%s,1,%s)',
                 (excluded, '苹果的服务业务是另一个独立证据。'))
    conn.commit()
    r.reset_stats_cache()
    found = search('苹果 -iPhone')
    assert found.total == found.available == sum(found.counts.values()) == 1
    assert [hit.article_id for hit in found.hits] == [excluded]


@pytest.mark.parametrize('page', ['999', str(2**63 - 1)])
def test_clamped_html_page_reloads_the_last_page(conn, settings, search_state, page):
    add, _search, _ = search_state
    identity = add('合成最后一页', 'clampneedle explains the mechanism.')
    with TestClient(create_app(settings)) as client:
        response = client.get('/search', params={'q': 'clampneedle', 'page': page})
    assert response.status_code == 200
    assert re.search(rf'href="/articles/{identity}(?:[?"#])', response.text)
    assert '没有找到' not in response.text and '1 篇全部命中' in response.text

"""合成独立测试库：文章的可达范围不能由段落候选上限决定。"""

from datetime import UTC, date, datetime

import pytest
from psycopg.types.json import Jsonb

from researchlib import retrieval
from researchlib.chunks import TRANSLATION_ORD_BASE
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.queries import Scope


@pytest.fixture
def search(conn, settings, monkeypatch):
    # 这些测试不依赖外部向量服务，也不触发付费模型。
    monkeypatch.setattr(retrieval, "semantic_candidates", lambda _c, _q, _s, rows: (rows, {}))
    sources = enabled_sources(load_sources(settings.sources_path))

    def run(text="paginationneedle", *, scope=None, **kwargs):
        return retrieval.search_articles(
            conn, text, scope or Scope(source_ids=[s.id for s in sources]), sources, **kwargs,
        )

    return run


def _bulk(conn, count, word="paginationneedle", *, source="fangwei-mp"):
    rows = conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)
        SELECT %(source)s, 'page:' || n, 'synthetic:' || n, '研究笔记 ' || n,
               '2024-06-01 00:00:00+00'::timestamptz,
               %(word)s || ' 是这篇研究中的经营机制，证据来自长期的供需变化。',
               %(word)s || ' 是这篇研究中的经营机制，证据来自长期的供需变化。'
        FROM generate_series(1, %(count)s) n RETURNING id
        """,
        {"count": count, "word": word, "source": source},
    ).fetchall()
    ids = [r["id"] for r in rows]
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, body)"
        " SELECT id, 0, body_text FROM articles WHERE id = ANY(%s)", (ids,),
    )
    conn.commit()
    return ids


def _article(conn, name, body="paginationneedle 长期供需变化解释了经营机制。", *, title=None,
             source="fangwei-mp", meta=None, published=datetime(2024, 6, 1, tzinfo=UTC), chunks=True):
    row = conn.execute(
        "INSERT INTO articles"
        " (source_id, external_id, content_hash, title, published_at, body_md, body_text, raw_meta)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (source, name, "synthetic:" + name, title or name, published, body, body, Jsonb(meta or {})),
    ).fetchone()
    if chunks:
        conn.execute("INSERT INTO article_chunks (article_id, ord, body) VALUES (%s, 0, %s)", (row["id"], body))
    conn.commit()
    return row["id"]


def _ids(result):
    return [hit.article_id for hit in result.hits]


def _tail_only(monkeypatch):
    monkeypatch.setattr(retrieval, "_candidates", lambda *_args, **_kwargs: [])


def test_more_than_2000_passages_pages_every_article_stably(conn, search):
    expected = set(_bulk(conn, 2407))
    seen = []
    for offset in range(0, len(expected), 127):
        result = search(limit=127, offset=offset)
        assert result.total == result.available == sum(result.counts.values()) == len(expected)
        assert result.partial == 0 and all(hit.full for hit in result.hits)
        assert len(result.hits) == min(127, len(expected) - offset)
        assert all(hit.passages and "paginationneedle" in hit.passages[0].body for hit in result.hits)
        seen.extend(_ids(result))
    assert len(seen) == len(set(seen)) and set(seen) == expected
    # 换分页大小、跨越相关度窗口和补齐区的边界，顺序仍相同。
    for offset in (0, 1993, 2399):
        assert _ids(search(limit=17, offset=offset)) == seen[offset:offset + 17]
        assert _ids(search(limit=17, offset=offset)) == seen[offset:offset + 17]
    assert search(offset=len(expected)).hits == []
    assert search(offset=len(expected) + 100).hits == []
    assert search(limit=0).hits == []
    assert _ids(search(limit=3, offset=-1)) == seen[:3]


def test_long_article_cannot_hide_other_articles_and_tail_text_is_bounded(conn, search, monkeypatch):
    word = "monopolyneedle"
    short = _bulk(conn, 401, word)
    long_id = _article(conn, "长研究", word, chunks=False)
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, body)"
        " SELECT %s, n, %s FROM generate_series(0, 2300) n", (long_id, word),
    )
    conn.commit()
    fetched = []
    original = retrieval._rows_for_keys

    def record(connection, keys):
        fetched.append(len(keys))
        return original(connection, keys)

    monkeypatch.setattr(retrieval, "_rows_for_keys", record)
    all_ids = []
    for offset in range(0, 402, 73):
        found = search(word, limit=73, offset=offset)
        assert found.total == found.available == 402
        all_ids.extend(_ids(found))
    assert len(all_ids) == len(set(all_ids)) and set(all_ids) == {*short, long_id}
    # 最后一页在补齐区：额外正文只来自本页文章；不会再把所有长文的块取回。
    fetched.clear()
    last = search(word, limit=7, offset=390)
    assert len(last.hits) == 7
    assert len(fetched) == 2
    assert fetched[0] <= 2300 and fetched[1] <= 7 * retrieval.SEARCH_SNIPPET_CANDIDATES


def test_tail_applies_source_author_dates_visibility_and_duplicates(conn, search, monkeypatch):
    _tail_only(monkeypatch)
    kept = _article(conn, "可见主版本")
    undated = _article(conn, "无日期", published=None)
    other = _article(conn, "另一个来源", source="gudujinhuati")
    duplicate = _article(conn, "重复版本")
    conn.execute("UPDATE articles SET duplicate_of = %s WHERE id = %s", (kept, duplicate))
    _article(conn, "隐藏", meta={"hidden": "short_post"})
    _article(conn, "无判断", meta={"suppressed": "empty_answer"})
    _article(conn, "读者帖", source="fangwei-zsxq", meta={"poster_role": "guest"})
    _article(conn, "评论壳", source="fangwei-zsxq", title="「图片」 评论（3）")
    old = _article(conn, "范围外", published=datetime(2023, 1, 1, tzinfo=UTC))
    conn.commit()
    found = search(limit=100)
    assert set(_ids(found)) == {kept, undated, other, old}
    assert _ids(found)[-1] == undated
    assert found.total == found.available == 4 and found.counts == {"fangwei-mp": 3, "gudujinhuati": 1}
    assert set(_ids(search(scope=Scope(source_ids=["fangwei-mp"])))) == {kept, undated, old}
    assert set(_ids(search("方伟 paginationneedle"))) == {kept, undated, old}
    scoped = Scope(source_ids=["fangwei-mp"], date_from=date(2024, 6, 1), date_to=date(2024, 6, 1))
    assert _ids(search(scope=scoped)) == [kept]
    assert _ids(search(scope=Scope(source_ids=["fangwei-mp"], article_id=duplicate))) == [duplicate]
    assert search(scope=Scope()).total == 0
    assert search("absentneedle").total == search("absentneedle").available == 0
    assert search("").hits == []


def test_tail_company_filters_or_groups_and_split_passage_matches(conn, search, monkeypatch):
    _tail_only(monkeypatch)
    ids = [_article(conn, f"提及{n}") for n in range(3)]
    companies = [conn.execute("SELECT id FROM companies WHERE name_zh = %s", (n,)).fetchone()["id"]
                 for n in ("腾讯", "京东")]
    for article, company in zip(ids[:2], companies, strict=True):
        conn.execute("INSERT INTO article_mentions VALUES (%s, %s, 'whole', 2)", (article, company))
    conn.commit()
    assert _ids(search("腾讯 paginationneedle")) == ids[:1]
    assert set(_ids(search("腾讯 or 京东"))) == set(ids[:2])
    assert set(_ids(search("腾讯 or paginationneedle"))) == set(ids)
    for article, company in zip(ids[:2], companies, strict=True):
        scope = Scope(source_ids=["fangwei-mp"], company_id=company)
        assert _ids(search(scope=scope)) == [article]
        assert _ids(search("腾讯 or 京东", scope=scope)) == [article]
    split = _article(conn, "分散在两节", "splitneedle\n\nsecondneedle", chunks=False)
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, body) VALUES (%s, 0, 'splitneedle'), (%s, 1, 'secondneedle')",
        (split, split),
    )
    conn.commit()
    result = search("splitneedle secondneedle")
    assert _ids(result) == [split] and result.hits[0].full and len(result.hits[0].passages) == 2


def test_tail_translation_title_only_missing_chunks_and_original_anchors(conn, search, monkeypatch):
    _tail_only(monkeypatch)
    translated = _article(conn, "English original", "Original language discusses unrelated topics.")
    conn.execute(
        "INSERT INTO article_translations"
        " (article_id, content_hash, title_zh, body_html, body_text, complete, segments)"
        " VALUES (%s, 'synthetic', '译文笔记', '<p>paginationneedle 的中文解释。</p>',"
        " 'paginationneedle 的中文解释。', true, 1)", (translated,),
    )
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, body, lang, anchor)"
        " VALUES (%s, %s, 'paginationneedle 的中文解释。', 'zh', 'p-6')",
        (translated, TRANSLATION_ORD_BASE + 5),
    )
    title_only = _article(conn, "标题中命中", "只介绍行业情况。", title="paginationneedle 标题")
    missing = _article(conn, "没有块的历史数据", chunks=False)
    conn.commit()
    result = search()
    assert result.total == result.available == 3 and set(_ids(result)) == {translated, title_only, missing}
    hit = next(h for h in result.hits if h.article_id == translated)
    assert hit.title == "译文笔记" and len(hit.passages) == 1
    passage = hit.passages[0]
    assert passage.lang == "zh" and passage.ord == TRANSLATION_ORD_BASE + 5
    assert passage.url.endswith(f"?chunk={TRANSLATION_ORD_BASE + 5}#p-6")
    assert next(h for h in result.hits if h.article_id == missing).passages == []


def test_indexed_full_candidate_is_not_misclassified_as_partial(conn, search):
    article = _article(conn, "两节的词合在一起才全", "splitneedle\n\nsecondneedle", chunks=False)
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, body) VALUES (%s, 0, 'splitneedle'), (%s, 1, 'secondneedle')",
        (article, article),
    )
    conn.commit()
    result = search("splitneedle secondneedle")
    assert _ids(result) == [article] and result.hits[0].full
    assert result.total == result.available == 1 and result.partial == 0


def test_tail_keeps_original_match_when_translation_does_not_match(conn, search, monkeypatch):
    _tail_only(monkeypatch)
    article = _article(conn, "英文原文命中", "paginationneedle explains the mechanism.")
    conn.execute(
        "INSERT INTO article_chunks (article_id, ord, body, lang, anchor)"
        " VALUES (%s, %s, '译文用了另一种说法。', 'zh', 'p-1')", (article, TRANSLATION_ORD_BASE),
    )
    conn.commit()
    hit = search().hits[0]
    assert hit.article_id == article and hit.passages[0].ord == 0
    assert "paginationneedle" in hit.passages[0].body


def test_quoted_and_excluded_words_do_not_reappear_in_tail_or_counts(conn, search, monkeypatch):
    _tail_only(monkeypatch)
    exact = _article(conn, "原样短语", "preciseneedle phrase 解释经营机制。")
    _article(conn, "标点隔开", "preciseneedle, phrase 解释经营机制。")
    _article(conn, "排除词", "preciseneedle phrase\n\nexcludeonly 在另一节。")
    similar = _article(conn, "相似但不是排除词", "preciseneedle phrase excludeonlylong")
    result = search('"preciseneedle phrase" -excludeonly')
    assert set(_ids(result)) == {exact, similar}
    assert result.total == sum(result.counts.values()) == result.available == 2


def test_semantic_promotions_are_counted_once_and_partial_starts_after_all_matches(conn, search, monkeypatch):
    lexical = set(_bulk(conn, 3))
    semantic = _article(conn, "按意思相近", "用另一种说法解释相同的供需关系。")
    partial = _article(conn, "弱相关", "提到相邻的主题。")
    rows = retrieval._rows_for_keys(conn, [(semantic, 0), (partial, 0)])
    _tail_only(monkeypatch)
    monkeypatch.setattr(
        retrieval, "semantic_candidates", lambda *_args, **_kwargs: (rows, {(semantic, 0): 0.9, (partial, 0): 0.4}),
    )
    result = search(limit=10)
    assert result.total == sum(result.counts.values()) == 4
    assert result.available == 5 and result.partial == 1
    assert _ids(result) == [semantic, *sorted(lexical, reverse=True), partial]
    assert all(h.full for h in result.hits[:4]) and result.hits[0].semantic
    assert not result.hits[-1].full
    for offset in range(7):
        page = search(limit=2, offset=offset)
        assert _ids(page) == _ids(result)[offset:offset + 2]
    # 同一篇也被语义一路召回时，和文章索引的并集只数一次。
    lexical_id = min(lexical)
    both = rows + retrieval._rows_for_keys(conn, [(lexical_id, 0)])
    monkeypatch.setattr(
        retrieval, "semantic_candidates",
        lambda *_args, **_kwargs: (both, {(semantic, 0): 0.9, (partial, 0): 0.4, (lexical_id, 0): 0.95}),
    )
    overlap = search(limit=10)
    assert overlap.total == sum(overlap.counts.values()) == 4 and overlap.available == 5
    assert len(_ids(overlap)) == len(set(_ids(overlap))) == 5

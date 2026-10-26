"""Native SQL checks use synthetic text and a fake embedder, never a service."""

import json
from pathlib import Path

import pytest
from test_long_and_semantic import FANGWEI, _doc, _fake_embed, _store
from test_long_and_semantic import fake_vectors as fake_vectors_fixture

from researchlib import embeddings
from researchlib.retrieval import FocusMatcher, Query, Term, _entity_alias_sql, _full_match_articles, _vector_keys
from researchlib.web.queries import Scope


@pytest.fixture
def fake_vectors(conn):
    yield from fake_vectors_fixture.__wrapped__(conn)


def recall(conn, limit):
    vector = embeddings.literal(_fake_embed(["会员费"])[0])
    with conn.transaction():
        return _vector_keys(conn, embeddings.column_type(conn), vector, Scope(source_ids=[FANGWEI]), limit)


def test_current_model_hash_and_existing_chunk_define_coverage_and_recall(conn, settings, fake_vectors):
    _store(conn, settings, FANGWEI, [
        _doc("fresh", "有效笔记", "会员费和毛利。"),
        _doc("stale", "修改笔记", "会员费和利润。"),
        _doc("other-model", "旧模型笔记", "会员费和低价。"),
    ])
    assert embeddings.backfill(conn) == 3
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM articles").fetchall()}
    conn.execute("UPDATE chunk_embeddings SET text_hash='stale' WHERE article_id=%s", (ids["stale"],))
    conn.execute("UPDATE chunk_embeddings SET model='other-model' WHERE article_id=%s", (ids["other-model"],))
    conn.execute(
        "INSERT INTO chunk_embeddings (article_id,ord,model,text_hash,embedding) "
        "SELECT article_id,77777,model,text_hash,embedding FROM chunk_embeddings WHERE article_id=%s",
        (ids["fresh"],),
    )
    assert embeddings.coverage(conn) == (1, 3)
    assert set(recall(conn, 1)) == {(ids["fresh"], 0)}
    assert set(recall(conn, 20)) == {(ids["fresh"], 0)}


def test_valid_article_does_not_authorize_unproven_cached_chunk(conn, settings, fake_vectors):
    _store(conn, settings, FANGWEI, [_doc("source", "原始笔记", "会员费和毛利。")])
    assert embeddings.backfill(conn) == 1
    aid = conn.execute("SELECT id FROM articles").fetchone()["id"]
    # This old translation has no current translation record/proof. The article
    # itself remains valid; a per-article filter alone would leak this chunk.
    conn.execute(
        "INSERT INTO article_chunks (article_id,ord,heading,body,lang) VALUES (%s,10000,'','未证翻译会员费','zh')",
        (aid,),
    )
    conn.execute(
        "INSERT INTO chunk_embeddings (article_id,ord,model,text_hash,embedding) "
        "SELECT article_id,10000,model,%s,embedding FROM chunk_embeddings WHERE article_id=%s AND ord=0",
        (embeddings.text_hash("原始笔记", "", "未证翻译会员费"), aid),
    )
    assert embeddings.coverage(conn) == (1, 1)
    assert set(recall(conn, 1)) == {(aid, 0)}
    assert set(recall(conn, 20)) == {(aid, 0)}


def test_entity_alias_sql_agrees_with_python_on_case_and_boundaries(conn):
    data = json.loads((Path(__file__).resolve().parents[1] / "fixtures/entity_alias_cases.json").read_text())
    statement = (
        f"SELECT {_entity_alias_sql('co', 'sample.body')} AS actual "
        "FROM (SELECT %s::text AS name_zh,%s::text AS name_en,%s::text AS ticker,%s::text[] AS aliases) co "
        "CROSS JOIN (SELECT %s::text AS body) sample"
    )
    mismatches = []
    for name, text, expected in data["cases"]:
        row = data["companies"][name]
        actual = conn.execute(statement, (row["name_zh"], row["name_en"], row["ticker"],
                                          row["aliases"], text)).fetchone()["actual"]
        python = bool(FocusMatcher([row]).count(text))
        if actual is not expected or python is not expected:
            mismatches.append((name, text, expected, actual, python))
    assert not mismatches, mismatches


def test_company_exact_and_vector_filters_distinguish_cost_in_title_and_body(conn, settings, fake_vectors):
    cases = [
        ("ordinary-title", "The Cost of AI", "Cloud operations demand careful planning.", False),
        ("ordinary-body", "Cloud economics", "The cost of AI keeps changing.", False),
        ("capitalized-body", "Operations", "Evaluating the Cost of AI requires careful accounting.", False),
        ("ticker-title", "COST earnings", "Membership renewal economics produce recurring revenue.", True),
        ("ticker-body", "Quarter notes", "Revenue at COST grew.", True),
        ("ticker-border", "Retail notes", "看好COST的会员费。", True),
        ("ticker-punctuation", "Membership model", "$COST: membership fees.", True),
        ("ticker-substring", "Forecast notes", "FORECOST and COST2 are not the exact ticker.", False),
        ("chinese-title", "开市客经营笔记", "门店长期以会员收入覆盖经营成本。", True),
        ("chinese-body", "Annual notes", "开市客的会员费。", True),
        ("brand", "costco earnings", "Warehouses rely on rapid inventory turnover.", True),
    ]
    _store(conn, settings, FANGWEI, [_doc(eid, title, body) for eid, title, body, _ in cases])
    companies = conn.execute("SELECT * FROM companies WHERE ticker='COST'").fetchall()
    assert len(companies) == 1
    company = companies[0]
    cid = company["id"]
    articles = conn.execute("SELECT id,external_id,duplicate_of FROM articles").fetchall()
    assert len(articles) == len(cases) and all(r["duplicate_of"] is None for r in articles)
    ids = {r["external_id"]: r["id"] for r in articles}
    expected = {ids[eid] for eid, _title, _body, keep in cases if keep}
    query = Query(raw="COST", terms=[Term("开市客", "company", 0, ["开市客", "Costco", "COST"], company_id=cid)],
                  companies=[company], authors=[], excludes=[], pairs=[], years=[], recent=False,
                  overview=False, timeline=False, author_intent=False)
    scope = Scope(source_ids=[FANGWEI])
    assert {r["id"] for r in _full_match_articles(conn, query, scope)} == expected
    assert embeddings.backfill(conn) == len(cases)
    with conn.transaction():
        keys = _vector_keys(conn, embeddings.column_type(conn), embeddings.literal(_fake_embed(["会员费"])[0]),
                            scope, 30, company_ids=[cid])
    assert {aid for aid, _ord in keys} == expected

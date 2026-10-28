"""用合成库和固定向量验证检索的一致性契约，不评估真实模型的语义效果。"""

import urllib.request
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest

from researchlib import ask, embeddings, retrieval
from researchlib.chunks import OCR_ORD_BASE, TRANSLATION_ORD_BASE, replace_chunks, replace_translation_chunks
from researchlib.sources import Source
from researchlib.textutil import content_hash, markdown_to_text
from researchlib.web.queries import Scope


@pytest.fixture(autouse=True)
def _no_external_models(monkeypatch):
    """意外调用外部服务即失败；各向量测试另行注入本地 stub。"""
    def reject(*_args, **_kwargs):
        raise AssertionError("一致性测试不得访问外部模型")

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    monkeypatch.setattr(ask, "chat", reject)
    embeddings.use_embedder(None)
    yield
    embeddings.use_embedder(None)


@pytest.fixture
def source(conn):
    item = Source("consistency-synthetic", "合成研究资料", "王小明", "markdown", True)
    conn.execute(
        "INSERT INTO sources(id,name,author,kind,enabled) VALUES (%s,%s,%s,%s,true)"
        " ON CONFLICT(id) DO UPDATE SET enabled=true",
        (item.id, item.name, item.author, item.kind),
    )
    conn.commit()
    return item


def _article(conn, source, external_id, title, body):
    when = datetime(2026, 1, 1, tzinfo=UTC)
    row = conn.execute(
        "INSERT INTO articles(source_id,external_id,title,body_md,body_text,content_hash,published_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (source.id, external_id, title, body, markdown_to_text(body), content_hash(title, body, when), when),
    ).fetchone()
    replace_chunks(conn, row["id"], title, body)
    conn.commit()
    return row


def _translated_article(conn, source):
    row = _article(
        conn, source, "bilingual", "Original field notebook",
        "The currentbodytoken describes a durable product and its distribution network.",
    )
    html = (
        "<h2>开头</h2><p>obsoletebodytoken 是合成译文的独有词，渠道销售保持稳定。</p>"
        "<h2>中段</h2><p>这一段解释合成公司的产品供应和客户留存。</p>"
        "<h2>结尾</h2><p>译文结尾记录经营现金流和独立的调研结论。</p>"
    )
    conn.execute(
        "INSERT INTO article_translations(article_id,content_hash,title_zh,body_html,body_text,complete,segments)"
        " VALUES (%s,%s,'obsoletetitletoken 译文标题',%s,%s,true,3)",
        (row["id"], row["content_hash"], html, markdown_to_text(html)),
    )
    assert replace_translation_chunks(conn, row["id"]) == 3
    conn.commit()
    return row


def _make_translation_stale(conn, article_id, cause):
    if cause == "hash":
        conn.execute("UPDATE articles SET content_hash=content_hash || '-changed' WHERE id=%s", (article_id,))
    else:
        conn.execute("DELETE FROM article_translations WHERE article_id=%s", (article_id,))
    conn.commit()
    # 故意不清理旧块，验证读取路径自行拒绝它们。
    assert conn.execute(
        "SELECT count(*) AS n FROM article_chunks WHERE article_id=%s AND lang='zh'", (article_id,)
    ).fetchone()["n"] == 3


@pytest.mark.parametrize("cause", ["hash", "missing"])
def test_stale_translation_is_absent_from_lexical_titles_and_whole_article(conn, source, cause):
    row = _translated_article(conn, source)
    scope = Scope(source_ids=[source.id])
    body_query = retrieval.analyze(conn, "obsoletebodytoken", [source])
    title_query = retrieval.analyze(conn, "obsoletetitletoken", [source])
    assert any(r["lang"] == "zh" for r in retrieval._candidates(conn, body_query, scope))
    assert (row["id"], TRANSLATION_ORD_BASE) in retrieval._title_keys(conn, title_query, scope, 10)

    _make_translation_stale(conn, row["id"], cause)

    assert retrieval._fts_keys(
        conn, scope, "plainto_tsquery('chinese', %(needle)s)", {"needle": "obsoletebodytoken"}, 10
    ) == []
    assert retrieval._title_keys(conn, title_query, scope, 10) == []
    assert not any(r["lang"] == "zh" for r in retrieval._candidates(conn, body_query, scope))
    assert retrieval._rows_for_keys(conn, [(row["id"], TRANSLATION_ORD_BASE)]) == []
    whole = retrieval._article_rows(conn, replace(scope, article_id=row["id"]))
    assert whole and all(r["lang"] == "" and r["title"] == row["title"] for r in whole)
    assert "currentbodytoken" in " ".join(r["body"] for r in whole)
    for term in ("obsoletebodytoken", "obsoletetitletoken"):
        found = retrieval.search_articles(conn, term, scope, [source])
        assert found.total == 0 and found.hits == []
    fresh = retrieval.retrieve(conn, "currentbodytoken", scope, [source], purpose="api")
    assert fresh.passages and all(p.lang != "zh" and p.title == row["title"] for p in fresh.passages)


@pytest.mark.parametrize("cause", ["hash", "missing"])
def test_stale_translation_is_not_added_as_context(conn, source, cause):
    row = _translated_article(conn, source)
    scope = Scope(source_ids=[source.id], article_id=row["id"])
    middle = next(r for r in retrieval._article_rows(conn, scope) if r["ord"] == TRANSLATION_ORD_BASE + 1)
    passage = retrieval._passage_from_row(middle)
    assert retrieval.attach_context(conn, [passage], 2000) > 0
    assert "obsoletebodytoken" in passage.before and "经营现金流" in passage.after

    _make_translation_stale(conn, row["id"], cause)

    # 模拟请求先选中段落，随后另一连接更新正文：旧译文的邻接块也不能再补进请求。
    passage = replace(passage, before="", after="")
    assert retrieval.attach_context(conn, [passage], 2000) == 0
    assert passage.before == passage.after == ""


def _stub_vectors(conn):
    if embeddings.column_type(conn) is None:
        pytest.skip("需要 pgvector，不能把缺少扩展算成通过")
    vector = [1.0] + [0.0] * (embeddings.DIM - 1)
    embeddings.use_embedder(lambda texts: [vector[:] for _ in texts], model="consistency-stub")
    return embeddings.column_type(conn), embeddings.literal(vector)


@pytest.mark.parametrize("cause", ["hash", "missing"])
def test_stale_translation_is_absent_from_vector_paths_and_backfill(conn, source, cause):
    row = _translated_article(conn, source)
    kind, literal = _stub_vectors(conn)
    assert embeddings.backfill(conn) == 4
    scope = Scope(source_ids=[source.id])
    stale_key = (row["id"], TRANSLATION_ORD_BASE)
    assert stale_key in retrieval._vector_keys(conn, kind, literal, scope, 10)
    keyword_rows = [{"article_id": stale_key[0], "ord": stale_key[1]}]
    assert stale_key in retrieval._vector_lookup(conn, kind, literal, scope, keyword_rows, 1, [])

    _make_translation_stale(conn, row["id"], cause)

    for current_scope in (scope, replace(scope, article_id=row["id"])):
        keys = retrieval._vector_keys(conn, kind, literal, current_scope, 10)
        assert keys and all(ord_ < TRANSLATION_ORD_BASE for _aid, ord_ in keys)
    assert stale_key not in retrieval._vector_lookup(conn, kind, literal, scope, keyword_rows, 1, [])
    conn.execute("DELETE FROM chunk_embeddings WHERE article_id=%s AND ord >= %s", stale_key)
    conn.commit()
    assert embeddings.backfill(conn) == 0
    assert embeddings.coverage(conn) == (1, 1)


def test_explicit_company_scope_survives_another_company_overview(conn, source):
    companies = {}
    for name in ("青禾科技", "远帆工业"):
        companies[name] = conn.execute(
            "INSERT INTO companies(name_zh,seeded) VALUES (%s,false) ON CONFLICT(name_zh)"
            " DO UPDATE SET name_zh=excluded.name_zh RETURNING *", (name,),
        ).fetchone()
    inside = _article(conn, source, "inside-company", "共同经营研究", "青禾科技与远帆工业合作，远帆工业新增订单。")
    outside = _article(conn, source, "outside-company", "另一家公司的独立研究", "远帆工业的产能利用率持续上升。")
    for article_id, company_id in (
        (inside["id"], companies["青禾科技"]["id"]),
        (inside["id"], companies["远帆工业"]["id"]),
        (outside["id"], companies["远帆工业"]["id"]),
    ):
        conn.execute(
            "INSERT INTO article_mentions(article_id,company_id,depth,hit_count) VALUES (%s,%s,'whole',2)",
            (article_id, company_id),
        )
    conn.commit()
    scope = Scope(source_ids=[source.id], company_id=companies["青禾科技"]["id"])
    overview = retrieval._company_rows(conn, companies["远帆工业"], scope, per_year=20)
    assert overview and {r["article_id"] for r in overview} == {inside["id"]}
    found = retrieval.retrieve(conn, "远帆工业", scope, [source])
    assert found.scope.company_id == companies["青禾科技"]["id"]
    assert {p.article_id for p in found.passages} == {inside["id"]}


def test_original_exact_words_translation_and_ocr_can_all_be_retrieved(conn, source):
    row = _translated_article(conn, source)
    ocr_text = "ocrfieldtoken 是截图独有的经营数据，记录了渠道库存和回款。"
    conn.execute(
        "INSERT INTO article_chunks(article_id,ord,heading,body,lang,anchor)"
        " VALUES (%s,%s,'图片里的字',%s,'ocr','image-synthetic')",
        (row["id"], OCR_ORD_BASE, ocr_text),
    )
    conn.commit()
    scope = Scope(source_ids=[source.id])
    for term, lang in (("currentbodytoken", ""), ("obsoletebodytoken", "zh"), ("ocrfieldtoken", "ocr")):
        found = retrieval.retrieve(conn, term, scope, [source], purpose="api")
        assert any(p.lang == lang and term in p.body for p in found.passages)
    whole = retrieval.retrieve(conn, "概括这篇", replace(scope, article_id=row["id"]), [source])
    assert whole.whole and {p.lang for p in whole.passages} == {"", "zh", "ocr"}


def test_long_article_without_headings_warns_about_incomplete_coverage(conn, source):
    body = "\n\n".join(f"第{index}段经营记录。" + "这是独立的渠道调查和产品经营资料。" * 35 for index in range(140))
    row = _article(conn, source, "long-unstructured", "无章节长文", body)
    found = retrieval.retrieve(conn, "概括这篇", Scope(source_ids=[source.id], article_id=row["id"]), [source])
    selected_chars = sum(len(p.body) for p in found.passages)
    assert not found.whole and found.outline == []
    assert 0 < selected_chars < found.total_chars
    note = ask.outline_note(
        found.outline, found.outline_label, whole=found.whole,
        total_chars=found.total_chars, selected_chars=selected_chars,
    )
    assert "部分原文" in note and "没有读完整篇" in note
    assert str(found.total_chars) in note and str(selected_chars) in note
    assert "不能判定为没有相关内容" in note
    assert ask.outline_note([], "", whole=True, total_chars=20, selected_chars=20) == ""


@pytest.mark.parametrize("broken", ["model", "body", "title", "heading", "deleted"])
def test_stale_embeddings_are_rejected_in_all_three_vector_paths(conn, source, broken):
    row = _article(conn, source, "vector-version", "合成向量研究", "青禾科技的客户续费稳定，渠道库存正在改善。")
    kind, literal = _stub_vectors(conn)
    assert embeddings.backfill(conn) == 1
    scope = Scope(source_ids=[source.id])
    assert retrieval._vector_keys(conn, kind, literal, scope, 1)
    if broken == "model":
        conn.execute("UPDATE chunk_embeddings SET model='obsolete-model'")
    elif broken == "title":
        conn.execute("UPDATE articles SET title=title || '修改后标题'")
    elif broken == "deleted":
        conn.execute("DELETE FROM article_chunks")
    else:
        conn.execute(f"UPDATE article_chunks SET {broken}={broken} || '修改后内容'")
    conn.commit()
    # 依次验证 ANN、指定文章的范围穷举，以及已有关键词候选的逐块补分。
    for current_scope in (scope, replace(scope, article_id=row["id"])):
        assert retrieval._vector_keys(conn, kind, literal, current_scope, 1) == {}
    assert retrieval._vector_lookup(
        conn, kind, literal, scope, [{"article_id": row["id"], "ord": 0}], 1, []
    ) == {}


def test_embedding_body_changed_during_model_call_is_not_written(conn, settings, source):
    row = _article(conn, source, "concurrent-vector", "合成并发研究", "青禾科技的客户续费稳定，渠道库存正在改善。")
    _stub_vectors(conn)
    calls = []

    def model(texts):
        calls.append(texts)
        # 独立连接在模型请求期间提交新正文，回填不能把旧请求的结果盖到当前块上。
        with psycopg.connect(settings.database_url) as other:
            other.execute("UPDATE article_chunks SET body=body || '请求期间的新正文' WHERE article_id=%s", (row["id"],))
        return [[1.0] + [0.0] * (embeddings.DIM - 1) for _ in texts]

    embeddings.use_embedder(model, model="consistency-concurrent-stub")
    skipped = []
    assert embeddings.backfill(conn, batch=1, skipped=skipped) == 0
    assert skipped == [(row["id"], 0)] and len(calls) == 1
    assert conn.execute("SELECT count(*) AS n FROM chunk_embeddings").fetchone()["n"] == 0

"""超长文、飞书目录和语义检索：几十万字的研究、「问这个目录」、搜词时按意思找。"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from researchlib import ask as ask_module
from researchlib import embeddings
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.retrieval import reset_stats_cache
from researchlib.sources import enabled_sources, load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app
from researchlib.web.queries import Scope

FEISHU = "linwenfeng-feishu"
FANGWEI = "fangwei-mp"
FILLER = "零售商要做的是供需匹配，周转决定了回报，规模带来议价，供应链要稳定，用户要信任。"


def _store(conn, settings, source_id, docs):
    source = next(item for item in load_sources(settings.sources_path) if item.id == source_id)
    store_documents(
        conn,
        source=source,
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )
    # 词频和块数缓存十分钟；换了一批文章要清掉，不然沿用上一个测试的统计。
    reset_stats_cache()


def _doc(external_id, title, body, *, month=1, feishu=False):
    meta = {"format": "markdown"}
    if feishu:
        meta.update({"parent": "docs", "filename": external_id.rsplit("/", 1)[-1]})
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=None if feishu else datetime(2024, month, 1, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta=meta,
        image_refs=[],
    )


def _long_body(sections: int, target: int, target_text: str) -> str:
    parts = []
    for index in range(sections):
        text = target_text if index == target else FILLER * 20
        parts.append(f"## 第 {index} 节 讨论零售\n\n{text}\n\n{FILLER * 6}")
    return "\n\n".join(parts)


def _body_under_single_call(target_text: str) -> tuple[str, int]:
    """生成切块后仍低于一次交给模型的字数上限的正文。返回 (正文, 节数)。

    上限看的是各块 _size（正文再加每块 80 字开销），不是原文字数。留出余量，
    避免贴着 SINGLE_CALL_CHARS、切块一变就走分批摘录。
    """
    from researchlib.chunks import make_chunks

    ceiling = ask_module.SINGLE_CALL_CHARS - 4_000
    chosen: tuple[str, int] | None = None
    for sections in range(8, 80):
        body = _long_body(sections, sections - 2, target_text)
        pieces = make_chunks("测量", body)
        size = sum(ask_module._size({"text": text}) for _, text in pieces)
        if size > ceiling:
            break
        chosen = (body, sections)
    if chosen is None:
        raise AssertionError(f"生成不出低于 {ask_module.SINGLE_CALL_CHARS} 字的正文")
    return chosen


def _sources(settings):
    return enabled_sources(load_sources(settings.sources_path))


def test_long_article_is_fully_indexed_and_ask_finds_the_late_section(conn, settings, monkeypatch):
    target = "拼多多的护城河是地球仪式的供应链调度，工厂按需求直接排产，库存周转天数很短。"
    body = _long_body(260, 250, target)
    assert len(body) > 250_000
    _store(conn, settings, FEISHU, [_doc("docs/拼多多投资研究__x.md", "拼多多投资研究", body, feishu=True)])
    article_id = conn.execute("SELECT id FROM articles").fetchone()["id"]
    chunks = conn.execute("SELECT count(*) AS n FROM article_chunks WHERE article_id = %s", (article_id,)).fetchone()
    # 以前每篇最多切 400 块，后面的内容进不了检索。
    assert chunks["n"] > 400

    seen = {}

    def fake_chat(_settings, messages, **_kwargs):
        if "检索词扩成" in messages[0]["content"]:
            return "{}", "grok-test"
        seen["messages"] = messages
        return "地球仪式的供应链调度 [1]。", "grok-test"

    monkeypatch.setattr(ask_module, "chat", fake_chat)
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    sources = _sources(settings)
    scope = Scope(source_ids=[s.id for s in sources], article_id=article_id)
    result = ask_module.ask(conn, settings, "地球仪供应链调度", scope, sources)
    assert result["status"] == "done"
    sent = seen["messages"][1]["content"]
    # 按问题定位到第 250 节，不是从开头截 6 万字。
    assert "地球仪式的供应链调度" in sent
    assert "第 0 节" not in sent.split("资料：", 1)[1]
    # 命中段带着前后文，前面附上全文的章节目录。
    assert "（上文）" in sent and "（下文）" in sent
    assert "这篇的章节如下" in sent and "第 199 节 讨论零售" in sent and "另有 60 条没列出" in sent


def test_ask_scoped_to_a_feishu_folder_only_reads_its_subtree(conn, settings, monkeypatch):
    from researchlib.doctree import build_doc_tree
    from researchlib.retrieval import retrieve

    _store(
        conn,
        settings,
        FEISHU,
        [
            _doc(
                "docs/主页-Jason 林文丰__root.md",
                "主页-Jason 林文丰",
                "[拼多多](拼多多投资研究__a.md)\n\n[茅台](茅台投资研究__b.md)\n",
                feishu=True,
            ),
            _doc(
                "docs/拼多多投资研究__a.md",
                "拼多多投资研究",
                "拼多多的总纲。\n\n[子资料](物流__c.md)\n",
                feishu=True,
            ),
            _doc("docs/茅台投资研究__b.md", "茅台投资研究", "茅台也讲飞轮效应和渠道库存。\n", feishu=True),
            _doc("docs/物流__c.md", "物流", "Temu 的飞轮效应来自全托管，物流目标是全球流转的确定性。\n", feishu=True),
        ],
    )
    build_doc_tree(conn, FEISHU)
    pdd = conn.execute("SELECT id FROM articles WHERE title = '拼多多投资研究'").fetchone()["id"]
    child = conn.execute("SELECT id FROM articles WHERE title = '物流'").fetchone()["id"]
    sources = _sources(settings)
    scope = Scope(source_ids=[s.id for s in sources], tree_id=pdd)
    found = retrieve(conn, "飞轮效应", scope, sources, purpose="llm")
    assert {p.article_id for p in found.passages} == {child}
    assert found.outline_label == "这个目录下的文章"
    assert found.outline[0] == "拼多多投资研究" and "物流" in found.outline

    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: False)
    with TestClient(create_app(settings)) as client:
        tree = client.get(f"/sources/{FEISHU}/tree/{pdd}").text
        assert f'href="/ask?tree={pdd}"' in tree and f'href="/ask?article={pdd}"' in tree
        leaf = client.get(f"/sources/{FEISHU}/tree/{child}").text
        assert "问这个目录" not in leaf
        page = client.get("/ask", params={"tree": str(pdd)}).text
        assert "只用这个目录" in page and f'name="tree" value="{pdd}"' in page
        asked = client.post("/ask", data={"q": "飞轮效应", "tree": str(pdd)}).text
        assert "全托管" in asked and "渠道库存" not in asked


# —— 语义检索：用一个按「概念」数词的假向量模型，不依赖真的向量服务 ——

CONCEPTS = [
    ("商业模式", "会员费", "毛利", "低价", "利润", "盈利"),
    ("开业", "排队", "门店", "开了新店"),
    ("天气", "下雨"),
]


def _fake_embed(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        vector = [0.0] * embeddings.DIM
        for index, words in enumerate(CONCEPTS):
            vector[index] = float(sum(text.count(word) for word in words))
        vector[-1] = 0.3
        vectors.append(vector)
    return vectors


@pytest.fixture
def fake_vectors(conn):
    if embeddings.column_type(conn) is None:
        pytest.skip("测试库没有 pgvector")
    embeddings.use_embedder(_fake_embed)
    yield
    embeddings.use_embedder(None)


def test_search_finds_articles_by_meaning_and_marks_them(conn, settings, fake_vectors):
    _store(
        conn,
        settings,
        FANGWEI,
        [
            _doc("model", "开市客笔记", "Costco 靠会员费赚钱，商品几乎不赚毛利，低价换来续费率。", month=1),
            _doc("store", "开市客新店", "Costco 在上海开了新店，门店外面排队很长，开业当天停车场满了。", month=2),
            _doc("rain", "天气", "今天下雨，天气转凉。", month=3),
        ],
    )
    assert embeddings.backfill(conn) == 3
    have, total = embeddings.coverage(conn)
    assert have == total == 3
    # 字改了才重算：再跑一遍什么都不做。
    assert embeddings.backfill(conn) == 0
    with TestClient(create_app(settings)) as client:
        page = client.get("/search", params={"q": "costco 商业模式"}).text
    model = page.find('href="/articles/1')
    store = page.find('href="/articles/2')
    # 「开市客笔记」一个「商业模式」都没写，按意思排到前面，并标出来。
    assert 0 <= model < store
    assert "语义相关" in page[:store]
    assert 'href="/articles/3' not in page


def test_semantic_channel_degrades_to_keywords_when_the_service_fails(conn, settings, fake_vectors):
    from researchlib.retrieval import retrieve

    _store(conn, settings, FANGWEI, [_doc("model", "开市客笔记", "Costco 靠会员费赚钱，商品几乎不赚毛利。")])
    embeddings.backfill(conn)

    def broken(_texts):
        raise embeddings.EmbeddingError("连不上")

    embeddings.use_embedder(broken)
    sources = _sources(settings)
    found = retrieve(conn, "开市客 会员费", Scope(source_ids=[s.id for s in sources]), sources, purpose="llm")
    assert found.passages and not any(p.semantic for p in found.passages)


def test_articles_cut_at_the_old_400_chunk_cap_are_rechunked(conn, settings):
    from researchlib.chunks import backfill_missing_chunks

    _store(conn, settings, FEISHU, [_doc("docs/长文__x.md", "长文", _long_body(260, 0, FILLER), feishu=True)])
    article_id = conn.execute("SELECT id FROM articles").fetchone()["id"]
    # 模拟旧版本切出来的样子：只有前 400 块。
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s AND ord >= 400", (article_id,))
    conn.commit()
    backfill_missing_chunks(conn)
    count = conn.execute("SELECT count(*) AS n FROM article_chunks WHERE article_id = %s", (article_id,)).fetchone()
    assert count["n"] > 400


def test_medium_article_reaches_the_model_whole(conn, settings, monkeypatch):
    from researchlib.retrieval import DISPLAY_ARTICLE_CHUNKS, retrieve

    # 一次交给模型的字数以内，但超过页面上列的块数。字数跟着 SINGLE_CALL_CHARS，
    # 不要写死：上限调低后四万字出头会走分批摘录，最后一次 chat 不再带「资料：」。
    target = "拼多多的护城河是地球仪式的供应链调度，工厂按需求直接排产，库存周转天数很短。"
    body, sections = _body_under_single_call(target)
    assert len(body) < ask_module.SINGLE_CALL_CHARS
    _store(conn, settings, FEISHU, [_doc("docs/拼多多投资研究__m.md", "拼多多投资研究", body, feishu=True)])
    article_id = conn.execute("SELECT id FROM articles").fetchone()["id"]
    chunks = conn.execute("SELECT count(*) AS n FROM article_chunks WHERE article_id = %s", (article_id,)).fetchone()
    assert chunks["n"] > DISPLAY_ARTICLE_CHUNKS

    seen = {}

    def fake_chat(_settings, messages, **_kwargs):
        if "检索词扩成" in messages[0]["content"]:
            return "{}", "grok-test"
        seen["messages"] = messages
        return "供应链调度 [1]。", "grok-test"

    monkeypatch.setattr(ask_module, "chat", fake_chat)
    monkeypatch.setattr(ask_module, "llm_configured", lambda _settings: True)
    sources = _sources(settings)
    scope = Scope(source_ids=[s.id for s in sources], article_id=article_id)
    result = ask_module.ask(conn, settings, "护城河是什么", scope, sources)
    assert result["status"] == "done"
    material = seen["messages"][1]["content"].split("资料：", 1)[1]
    # 以前交给模型的也截在 40 块（两万字出头），后半篇读不到，又被当成全文、不附目录，模型只能答「没有」。
    assert target in material
    assert "第 0 节" in material and f"第 {sections - 1} 节" in material
    assert "这篇的章节如下" not in seen["messages"][1]["content"]
    # 页面上的「相关原文」照旧只列前面这些块。
    shown = retrieve(conn, "护城河是什么", scope, sources, purpose="display")
    assert len(shown.passages) == DISPLAY_ARTICLE_CHUNKS


def test_vector_recall_filters_after_the_index_and_falls_back_when_too_few_left(conn, settings, fake_vectors):
    from researchlib.retrieval import _vector_keys

    # 方伟这边 8 篇，从「讲会员费」渐变到「讲门店」；林文丰飞书 2 篇，一篇和第 0 篇几乎一样近，一篇讲天气。
    _store(
        conn,
        settings,
        FANGWEI,
        [_doc(f"m{i}", f"笔记 {i}", "会员费，" * (8 - i) + "门店，" * i, month=i + 1) for i in range(8)],
    )
    _store(
        conn,
        settings,
        FEISHU,
        [
            _doc("docs/会员__f.md", "会员", "会员费，" * 9, feishu=True),
            _doc("docs/天气__w.md", "天气", "今天下雨，天气转凉。", feishu=True),
        ],
    )
    assert embeddings.backfill(conn) == 10
    titles = {row["id"]: row["title"] for row in conn.execute("SELECT id, title FROM articles").fetchall()}
    kind = embeddings.column_type(conn)
    vector = embeddings.literal(_fake_embed(["商业模式"])[0])

    def keys(source_ids, limit):
        with conn.transaction():
            found = _vector_keys(conn, kind, vector, Scope(source_ids=source_ids), limit)
        return {titles[article_id] for article_id, _ord in found}

    both = [FANGWEI, FEISHU]
    assert keys(both, 3) == {"笔记 0", "会员", "笔记 1"}
    # 近邻里排第二的「会员」不在范围里，过滤掉后还够数。
    assert keys([FANGWEI], 2) == {"笔记 0", "笔记 1"}
    # 范围里只有一篇挤进近邻，不够数：逐块算，离得很远的「天气」也找得到。
    assert keys([FEISHU], 2) == {"会员", "天气"}
    # 搁下的文章即使离得最近也不出来。
    first = next(article_id for article_id, title in titles.items() if title == "笔记 0")
    conn.execute(
        "UPDATE articles SET raw_meta = raw_meta || '{\"suppressed\": \"test\"}'::jsonb WHERE id = %s", (first,)
    )
    assert keys(both, 2) == {"会员", "笔记 1"}


def test_backfill_skips_a_chunk_the_service_rejects_and_keeps_going(conn, settings):
    if embeddings.column_type(conn) is None:
        pytest.skip("测试库没有 pgvector")

    def picky(texts):
        if any("乱码" in text for text in texts):
            raise embeddings.EmbeddingError("向量服务返回 500")
        return _fake_embed(texts)

    def down(_texts):
        raise embeddings.EmbeddingError("连不上")

    _store(
        conn,
        settings,
        FANGWEI,
        [
            _doc("new", "新的一篇", "Costco 的会员费。", month=3),
            _doc("bad", "坏的一篇", "乱码", month=2),
            _doc("old", "旧的一篇", "下雨天。", month=1),
        ],
    )
    bad = conn.execute("SELECT id FROM articles WHERE title = '坏的一篇'").fetchone()["id"]
    embeddings.use_embedder(picky)
    try:
        skipped: list[tuple[int, int]] = []
        # 一批只算一块：坏块单独成一批也不能卡住后面的。以前整批报错就退出，下一轮又从这块开始，旧文章永远算不到。
        assert embeddings.backfill(conn, batch=1, skipped=skipped) == 2
        assert skipped == [(bad, 0)]
        assert embeddings.coverage(conn) == (2, 3)
        # 服务本身停了照旧报错，不把每一块都当坏块跳过。
        embeddings.use_embedder(down)
        with pytest.raises(embeddings.EmbeddingError):
            embeddings.backfill(conn)
    finally:
        embeddings.use_embedder(None)


def test_search_page_says_when_meaning_search_is_not_in_use(conn, settings):
    _store(
        conn,
        settings,
        FANGWEI,
        [
            _doc("model", "开市客笔记", "Costco 靠会员费赚钱，商品几乎不赚毛利。", month=1),
            _doc("store", "开市客新店", "Costco 在上海开了新店，门店外面排队很长。", month=2),
            _doc("rain", "天气", "今天下雨，天气转凉。", month=3),
        ],
    )
    embeddings.use_embedder(None)
    with TestClient(create_app(settings)) as client:
        assert "没开语义检索" in client.get("/search", params={"q": "costco 商业模式"}).text
        # 只搜公司名不走语义这一路，不提。
        assert "semantic-note" not in client.get("/search", params={"q": "costco"}).text
    if embeddings.column_type(conn) is None:
        return
    embeddings.use_embedder(_fake_embed)
    try:
        with TestClient(create_app(settings)) as client:

            def page() -> str:
                return client.get("/search", params={"q": "costco 商业模式"}).text

            assert "向量还一块都没算" in page()
            embeddings.backfill(conn, limit=1)
            assert "向量还没算完（1/3 块）" in page()
            embeddings.backfill(conn)
            assert "semantic-note" not in page()
            embeddings._state.down_until = time.monotonic() + 60
            assert "向量服务连不上" in page()
    finally:
        embeddings.use_embedder(None)

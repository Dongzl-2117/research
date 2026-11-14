"""改段落时向量跟着一起换：检索看到的要么是旧段落和旧向量，要么是新段落和新向量。

11-11 research-clean 改了约 6000 篇的段落以后，向量要等 research-embed 重算，中间检索页报「向量还没算完」，
这些段落只能按字面找到，融合分只剩关键词那一半。全库重译时每译完一篇，中文段落也要等到夜里。
这里的向量都是按文字算的合成值，不调真的向量服务。
"""

from __future__ import annotations

import hashlib
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from researchlib import embeddings
from researchlib.boilerplate import EMPTY, rewash
from researchlib.chunks import TRANSLATION_ORD_BASE, replace_chunks, replace_translation_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources

FEISHU = "linwenfeng-feishu"


def _vector(text: str) -> list[float]:
    """按文字定下的合成向量：字一样，向量就一样。"""
    seed = hashlib.sha256(text.encode()).digest()
    return [((seed[index % len(seed)] + index) % 97) / 97.0 + 0.01 for index in range(embeddings.DIM)]


class Service:
    """记下每次算了哪些字。down=True 时像服务停了一样报错。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.down = False

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if self.down:
            raise embeddings.EmbeddingError("向量服务连不上（合成）")
        self.texts.extend(texts)
        return [_vector(text) for text in texts]


@pytest.fixture
def service(conn):
    if embeddings.column_type(conn) is None:
        pytest.skip("测试库没有 pgvector")
    fake = Service()
    embeddings.use_embedder(fake, model="synthetic")
    yield fake
    embeddings.use_embedder(None)


def _paragraph(index: int) -> str:
    # 一段五六百字，切块时自成一块（TARGET_CHARS 500，MAX_CHARS 800）。
    return f"第{index}段讲零售的第{index}个问题：" + f"会员费和周转决定回报，规模带来议价，第{index}条。" * 22


def _store(conn, settings, body: str, title: str = "零售研究") -> int:
    source = next(item for item in load_sources(settings.sources_path) if item.id == FEISHU)
    store_documents(
        conn,
        source=source,
        docs=[
            ParsedDoc(
                external_id="docs/零售研究__r.md",
                title=title,
                published_at=None,
                body_md=body,
                raw_meta={"format": "markdown", "parent": "docs", "filename": "零售研究__r.md"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    return conn.execute("SELECT id FROM articles").fetchone()["id"]


def _bodies(conn, article_id: int, lo: int = 0, hi: int = TRANSLATION_ORD_BASE) -> list[str]:
    return [
        row["body"]
        for row in conn.execute(
            "SELECT body FROM article_chunks WHERE article_id = %s AND ord >= %s AND ord < %s ORDER BY ord",
            (article_id, lo, hi),
        ).fetchall()
    ]


def _vectors(conn, article_id: int) -> dict[int, str]:
    return {
        row["ord"]: row["v"]
        for row in conn.execute(
            "SELECT ord, embedding::text AS v FROM chunk_embeddings WHERE article_id = %s", (article_id,)
        ).fetchall()
    }


def test_rewritten_paragraphs_and_their_vectors_land_in_one_commit(conn, settings, database_url, service):
    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(3)))
    # 入库时就算好了：不用等 research-embed。
    assert embeddings.coverage(conn) == (3, 3)
    old = _bodies(conn, article_id)
    reader = psycopg.connect(database_url, row_factory=dict_row)
    try:
        # 清洗改了第 0 段（比如去掉文末推销），这一篇的段落重切，还没提交。
        new_body = "\n\n".join([_paragraph(0).replace("第0条", "修订后"), _paragraph(1), _paragraph(2)])
        replace_chunks(conn, article_id, "零售研究", new_body)
        # 别的连接（网站的检索）这时看到的还是完整的旧版：旧段落，每段都有对得上的向量。
        assert _bodies(reader, article_id) == old
        assert embeddings.coverage(reader) == (3, 3)
        reader.commit()
        conn.commit()
        # 提交以后是完整的新版：新段落，每段都有对得上的向量，中间没有「向量还没算完」。
        assert _bodies(reader, article_id) != old
        assert embeddings.coverage(reader) == (3, 3)
        assert embeddings.status_note(reader) == ""
    finally:
        reader.close()


def test_unchanged_paragraphs_keep_their_vectors_when_block_numbers_shift(conn, settings, service):
    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(4)))
    before = _vectors(conn, article_id)
    service.texts.clear()
    # 开头那段被洗掉：后面三段原样往前挪一个块号。
    replace_chunks(conn, article_id, "零售研究", "\n\n".join(_paragraph(i) for i in range(1, 4)))
    conn.commit()
    # 字没变的段不重算，照抄原来的向量；多出来的块号上的向量删掉。
    assert service.texts == []
    after = _vectors(conn, article_id)
    assert after == {0: before[1], 1: before[2], 2: before[3]}
    assert embeddings.coverage(conn) == (3, 3)
    # 只有改了字的段才算。
    edited = [_paragraph(1), _paragraph(2).replace("第2条", "新写的"), _paragraph(3)]
    replace_chunks(conn, article_id, "零售研究", "\n\n".join(edited))
    conn.commit()
    assert len(service.texts) == 1 and "新写的" in service.texts[0]
    assert embeddings.coverage(conn) == (3, 3)


def test_new_translation_is_searchable_by_meaning_right_away(conn, settings, service):
    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(2)))
    original = {ord_: vector for ord_, vector in _vectors(conn, article_id).items() if ord_ < TRANSLATION_ORD_BASE}
    service.texts.clear()
    digest = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (article_id,)).fetchone()["content_hash"]
    zh = "<h2>零售</h2><p>会员费和周转决定回报。</p><p>规模带来议价。</p>"
    conn.execute(
        "INSERT INTO article_translations"
        " (article_id, content_hash, title_zh, body_html, body_text, complete, segments)"
        " VALUES (%s, %s, '零售研究', %s, %s, true, 2)",
        (article_id, digest, zh, zh),
    )
    replace_translation_chunks(conn, article_id)
    conn.commit()
    # 译文块一写进去就有向量（以前要等夜里的 research-embed），原文块的向量不动、不重算。
    assert service.texts and all("会员费和周转决定回报。" in text for text in service.texts)
    assert {o: v for o, v in _vectors(conn, article_id).items() if o < TRANSLATION_ORD_BASE} == original
    have, total = embeddings.coverage(conn)
    assert have == total == 3


def test_clean_command_stops_without_touching_anything_when_the_service_is_down(conn, settings, service):
    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(2)))
    # 清洗规则变了：库里的检索文字和段落还是旧规则洗的。
    conn.execute("UPDATE articles SET body_text = '旧规则洗出来的检索文字' WHERE id = %s", (article_id,))
    conn.execute("UPDATE article_chunks SET body = body || '（旧规则留下的推销）' WHERE article_id = %s", (article_id,))
    conn.commit()
    embeddings.backfill(conn)
    old = _bodies(conn, article_id)
    service.down = True
    with embeddings.all_or_nothing(), pytest.raises(embeddings.EmbeddingError):
        rewash(conn, [FEISHU])
    conn.rollback()
    # 什么都没换：检索文字、段落、向量都是完整的旧版。
    row = conn.execute("SELECT body_text FROM articles WHERE id = %s", (article_id,)).fetchone()
    assert row["body_text"] == "旧规则洗出来的检索文字"
    assert _bodies(conn, article_id) == old
    assert embeddings.coverage(conn) == (2, 2)
    # 服务好了再跑一遍：字和向量一起换好。
    service.down = False
    with embeddings.all_or_nothing():
        report = rewash(conn, [FEISHU])
    assert report["changed"] == {FEISHU: 1}
    assert all("旧规则留下的推销" not in body for body in _bodies(conn, article_id))
    assert embeddings.coverage(conn) == (2, 2)


def test_jobs_that_paid_for_their_output_save_it_and_leave_vectors_for_later(conn, settings, service):
    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(2)))
    service.texts.clear()
    service.down = True
    # 译制、识图、导入不包 all_or_nothing：服务停了也照常存，缺的向量留给 research-embed。
    replace_chunks(conn, article_id, "零售研究", _paragraph(0).replace("第0条", "改过"))
    conn.commit()
    assert "改过" in _bodies(conn, article_id)[0]
    assert embeddings.coverage(conn) == (0, 1)
    # 刚失败过，接下来一阵子不再一篇篇地等超时。
    service.down = False
    replace_chunks(conn, article_id, "零售研究", _paragraph(0).replace("第0条", "又改过"))
    conn.commit()
    assert service.texts == []
    # 服务好了，research-embed 补上。
    embeddings._state.sync_down_until = time.monotonic() - 1
    assert embeddings.backfill(conn) == 1
    assert embeddings.coverage(conn) == (1, 1)


def test_backfill_reuses_vectors_of_paragraphs_that_only_moved(conn, settings, service):
    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(3)))
    before = _vectors(conn, article_id)
    # 当时向量服务没开，段落先换了：开头那段删掉，后面的往前挪。
    embeddings.use_embedder(None)
    replace_chunks(conn, article_id, "零售研究", "\n\n".join(_paragraph(i) for i in range(1, 3)))
    conn.commit()
    fake = Service()
    embeddings.use_embedder(fake, model="synthetic")
    # research-embed 先照抄同一篇里字一样的向量，一块都不用重算。
    assert embeddings.backfill(conn) == 0
    assert fake.texts == []
    assert _vectors(conn, article_id) == {0: before[1], 1: before[2]}
    assert embeddings.coverage(conn) == (2, 2)


def test_title_change_refreshes_vectors_of_the_whole_article(conn, settings, service):
    from researchlib.zsxq_repair import _rewrite_chunks

    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(2)))
    conn.execute("UPDATE articles SET title = '新标题' WHERE id = %s", (article_id,))
    service.texts.clear()
    # 向量的输入带标题：标题改了，每一块都要重算。
    body = conn.execute("SELECT body_md FROM articles WHERE id = %s", (article_id,)).fetchone()["body_md"]
    _rewrite_chunks(conn, article_id, "新标题", body, EMPTY, {})
    conn.commit()
    assert len(service.texts) == 2 and all(text.startswith("新标题\n") for text in service.texts)
    assert embeddings.coverage(conn) == (2, 2)


def test_new_title_on_reimport_refreshes_translation_vectors_too(conn, settings, service):
    """重新导入只改了标题、译文沿用：向量的输入带标题，译文块的向量当场一起换，不等夜里。"""
    body = "\n\n".join(_paragraph(i) for i in range(2))
    article_id = _store(conn, settings, body)
    digest = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (article_id,)).fetchone()["content_hash"]
    zh = "<p>会员费和周转决定回报。</p><p>规模带来议价。</p>"
    conn.execute(
        "INSERT INTO article_translations"
        " (article_id, content_hash, title_zh, body_html, body_text, complete, segments)"
        " VALUES (%s, %s, '零售研究', %s, %s, true, 2)",
        (article_id, digest, zh, zh),
    )
    replace_translation_chunks(conn, article_id)
    conn.commit()
    have, total = embeddings.coverage(conn)
    assert have == total and total > 2
    _store(conn, settings, body, title="零售研究（修订）")
    assert embeddings.coverage(conn) == (total, total)


def test_clean_command_stops_when_the_vector_service_is_not_configured(conn, settings, service):
    """库里有向量、这次却没配向量服务：清洗、重切和服务停了一样停下，不提交没向量的段落。"""
    article_id = _store(conn, settings, "\n\n".join(_paragraph(i) for i in range(2)))
    old = _bodies(conn, article_id)
    embeddings.use_embedder(None)
    with embeddings.all_or_nothing(), pytest.raises(embeddings.EmbeddingError):
        replace_chunks(conn, article_id, "零售研究", _paragraph(0).replace("第0条", "改过"))
    conn.rollback()
    assert _bodies(conn, article_id) == old
    # 不在 all_or_nothing 里（译制、识图、导入）照常存。
    replace_chunks(conn, article_id, "零售研究", _paragraph(0).replace("第0条", "改过"))
    conn.commit()
    assert "改过" in _bodies(conn, article_id)[0]

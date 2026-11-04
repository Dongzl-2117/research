"""语义召回只读当前输入和当前模型的向量；所有向量为固定合成值。"""

from dataclasses import replace
from datetime import UTC, datetime

from researchlib import embeddings, retrieval
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.queries import Scope


def test_vector_paths_and_coverage_exclude_stale_or_other_model_vectors(conn, settings):
    source = replace(next(s for s in load_sources(settings.sources_path) if s.id == 'fangwei-mp'), min_chars=0)
    for index in range(3):
        store_documents(conn, source=source, docs=[ParsedDoc(
            f'synthetic-vector-{index}', f'合成向量{index}', datetime(2026, 1, index + 1, tzinfo=UTC),
            f'合成资料第{index}条，与其它条目使用不同的正文。', {}, [],
        )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    vector = [1.0] + [0.0] * (embeddings.DIM - 1)
    embeddings.use_embedder(lambda texts: [vector for _ in texts], model='synthetic-current')
    try:
        assert embeddings.backfill(conn) == 3
        ids = [row['id'] for row in conn.execute('SELECT id FROM articles ORDER BY id').fetchall()]
        conn.execute("UPDATE chunk_embeddings SET text_hash='stale' WHERE article_id=%s", (ids[0],))
        conn.execute("UPDATE chunk_embeddings SET model='synthetic-other' WHERE article_id=%s", (ids[1],))
        conn.commit()
        scope = Scope(source_ids=['fangwei-mp'])
        kind, literal = embeddings.column_type(conn), embeddings.literal(vector)
        with conn.transaction():
            found = retrieval._vector_keys(conn, kind, literal, scope, 10)
        assert set(found) == {(ids[2], 0)}
        rows = conn.execute('SELECT article_id,ord FROM article_chunks ORDER BY article_id').fetchall()
        with conn.transaction():
            rescored = retrieval._vector_lookup(conn, kind, literal, scope, rows, 10, [])
        assert set(rescored) == {(ids[2], 0)}
        assert embeddings.coverage(conn) == (1, 3)
        conn.execute("UPDATE article_chunks SET body='合成修订后的正文' WHERE article_id=%s", (ids[2],))
        conn.commit()
        with conn.transaction():
            assert retrieval._vector_keys(conn, kind, literal, scope, 10) == {}
        assert embeddings.coverage(conn) == (0, 3)
    finally:
        embeddings.use_embedder(None)

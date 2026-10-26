"""A coarse SQL OCR candidate cannot stop later fully proven text."""

from test_long_and_semantic import FANGWEI, _doc, _store

from researchlib import embeddings


def test_reordered_ocr_head_is_skipped_while_later_valid_text_is_embedded(conn, settings):
    _store(conn, settings, FANGWEI, [
        _doc("ocr-head", "旧图像片段", "![figure](chart.png)", month=2),
        _doc("valid-later", "会员费笔记", "会员费支撑低毛利经营。", month=1),
    ])
    ids = {r["external_id"]: r["id"] for r in conn.execute("SELECT id, external_id FROM articles")}
    bad, good = ids["ocr-head"], ids["valid-later"]
    image_hash = "e" * 64
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s", (bad,))
    conn.execute(
        "UPDATE articles SET ocr_text = 'beta alpha', raw_meta = raw_meta || '{\"ocr_role_version\":4}'::jsonb "
        "WHERE id = %s", (bad,),
    )
    conn.execute(
        "INSERT INTO article_images (article_id,source_id,original_ref,storage_path,content_hash) "
        "VALUES (%s,%s,'chart.png','synthetic.png',%s)", (bad, FANGWEI, image_hash),
    )
    conn.execute("INSERT INTO image_texts (content_hash,engine,text) VALUES (%s,'synthetic','alpha beta')",
                 (image_hash,))
    conn.execute(
        "INSERT INTO article_chunks (article_id,ord,heading,body,lang,anchor) "
        "VALUES (%s,20000,'图片里的字','beta alpha','ocr',%s)", (bad, "img-" + image_hash[:12]),
    )
    conn.commit()
    sent, skipped = [], []

    def fake_embed(texts):
        sent.extend(texts)
        return [[1.] + [0.] * (embeddings.DIM - 1) for _ in texts]

    embeddings.use_embedder(fake_embed)
    try:
        head = embeddings._pending(conn, 1, embeddings.model_name())
        assert [(r["article_id"], r["ord"]) for r in head] == [(bad, 20000)]
        assert embeddings._current_rows(conn, head) == []
        assert embeddings.backfill(conn, batch=1, skipped=skipped) == 1
        assert skipped == [(bad, 20000)]
        assert len(sent) == 1 and "会员费" in sent[0] and "beta" not in sent[0]
        assert [(r["article_id"], r["ord"]) for r in conn.execute("SELECT article_id,ord FROM chunk_embeddings")] == [
            (good, 0)
        ]
    finally:
        embeddings.use_embedder(None)

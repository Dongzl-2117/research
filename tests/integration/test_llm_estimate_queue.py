"""回填估费与真正待写的摘要、公司判断使用同一批回执。"""

from researchlib import digest, llm_cost, views
from researchlib.seed import load_sample


def test_estimate_follows_completed_and_expired_work(conn, settings, monkeypatch):
    monkeypatch.setenv("ALLOW_SAMPLE_SEED", "1")
    load_sample(conn, settings)
    row = conn.execute(
        "SELECT id, source_id, content_hash FROM articles WHERE external_id='sample:tencent-ads'"
    ).fetchone()
    article_id = row["id"]

    def estimate():
        rows = llm_cost.load_db_rows(conn, source_id=row["source_id"], article_ids=[article_id])
        [spec] = llm_cost.specs_from_rows(rows)
        queue = digest.pending_articles(conn, [row["source_id"]], 50)
        assert spec.need_digest == any(job["id"] == article_id for job in queue)
        assert spec.deep_mentions == len(views.pending_companies(conn, article_id))
        return spec

    assert estimate().need_digest
    companies = views.pending_companies(conn, article_id)
    assert companies
    conn.execute(
        "INSERT INTO article_digests (article_id, content_hash, prompt_version, model, summary)"
        " VALUES (%s, %s, %s, 'test', '摘要')",
        (article_id, row["content_hash"], digest.PROMPT_VERSION),
    )
    for company in companies:
        conn.execute("INSERT INTO company_views (article_id, company_id, content_hash, prompt_version)"
                     " VALUES (%s,%s,%s,%s)", (article_id, company["id"], row["content_hash"], views.PROMPT_VERSION))
    current = estimate()
    assert not current.need_digest and current.deep_mentions == 0
    # 已读开头的摘要仍须补读。只过期一家公司的判断时，仍只算这篇一次。
    conn.execute("UPDATE article_digests SET head_only=true WHERE article_id=%s", (article_id,))
    conn.execute("UPDATE company_views SET content_hash='旧正文' WHERE article_id=%s AND company_id=%s",
                 (article_id, companies[0]["id"]))
    expired = estimate()
    assert expired.need_digest and expired.deep_mentions == 1
    # 图片里读到的资料能独立触发摘要和判断，哪怕正文只有「见图」、公司只是 passing。
    conn.execute("UPDATE articles SET body_text='见图', ocr_text=%s, ocr_hash='新图' WHERE id=%s",
                 ("公司的营收与渠道周转情况。" * 10, article_id))
    conn.execute("UPDATE article_mentions SET depth='passing' WHERE article_id=%s", (article_id,))
    ocr = estimate()
    assert ocr.need_digest and ocr.deep_mentions > 0 and ocr.chars > 100

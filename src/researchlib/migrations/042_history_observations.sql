-- 网页/API 看到某个版本的最后时刻。编辑事务尚未提交时，旧版仍可能被另一个会话实际读到。
CREATE TABLE IF NOT EXISTS article_evidence_observations (
    article_id bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    written_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (article_id, written_at)
);

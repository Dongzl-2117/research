-- 识图把短正文的图片文字写上以后，摘要和星球标题要重做。
-- 只加一张空队列表，不改已有文章、摘要或标题。

CREATE TABLE IF NOT EXISTS article_followups (
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  kind text NOT NULL,
  reason text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (article_id, kind)
);

CREATE INDEX IF NOT EXISTS article_followups_kind_created
  ON article_followups (kind, created_at, article_id);

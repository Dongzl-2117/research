-- 改稿和删稿。重新导入时正文变了：旧版先存进这里（previous），再替换成新正文。
-- 新正文像删除提示（「该内容已被发布者删除」这类），或者比原来短一半以上：不替换，
-- 只把这一版记下来（held），库里留的仍是完整原文。同一版不重复记。

CREATE TABLE IF NOT EXISTS article_revisions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('previous', 'held')),
  reason text NOT NULL DEFAULT '',
  content_hash text NOT NULL,
  title text NOT NULL,
  published_at timestamptz,
  body_md text NOT NULL,
  body_text text NOT NULL,
  raw_meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (article_id, content_hash)
);

CREATE INDEX IF NOT EXISTS article_revisions_article_idx ON article_revisions (article_id, created_at);

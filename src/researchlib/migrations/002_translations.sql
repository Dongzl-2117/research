-- 英文原文的中文译文。和 AIHOT 一样：译文是与原文同结构的 HTML，站内再按顶层块做中英对照。
-- 中文来源不写本表。incomplete 表示部分段落未译成，对照页保留原文。

CREATE TABLE article_translations (
  article_id bigint PRIMARY KEY REFERENCES articles (id) ON DELETE CASCADE,
  lang text NOT NULL DEFAULT 'zh' CHECK (lang = 'zh'),
  content_hash text NOT NULL,
  title_zh text NOT NULL DEFAULT '',
  body_html text NOT NULL,
  body_text text NOT NULL,
  complete boolean NOT NULL DEFAULT false,
  origin text NOT NULL DEFAULT 'model' CHECK (origin IN ('model', 'source', 'manual')),
  segments integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX article_translations_pending_idx
  ON article_translations (complete, updated_at DESC);

COMMENT ON TABLE article_translations IS
  'Chinese body for a foreign-language article. Paired with rendered body_md on the article page.';

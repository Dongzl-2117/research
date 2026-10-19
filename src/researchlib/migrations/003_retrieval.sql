-- 段落块、LLM 摘要和问答记录。articles 表本身不改。
-- 排在 002_translations.sql 之后：先有译文表，再有检索表。
-- 段落块给检索片段和问答引用用：一篇长文切成几百字一块，引用能落到具体段落。
-- 本迁移只建表。全库摘要不在迁移或进程启动时跑，只通过 research-digest 手动执行。

CREATE TABLE article_chunks (
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  ord integer NOT NULL,
  heading text NOT NULL DEFAULT '',
  body text NOT NULL,
  search_tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('chinese', coalesce(heading, '')), 'A')
    || setweight(to_tsvector('chinese', coalesce(body, '')), 'B')
  ) STORED,
  PRIMARY KEY (article_id, ord)
);

CREATE INDEX article_chunks_search_tsv_idx ON article_chunks USING gin (search_tsv);

-- content_hash 与 articles.content_hash 对不上，说明正文改过，摘要要重算。
CREATE TABLE article_digests (
  article_id bigint PRIMARY KEY REFERENCES articles (id) ON DELETE CASCADE,
  content_hash text NOT NULL,
  model text NOT NULL,
  summary text NOT NULL,
  points jsonb NOT NULL DEFAULT '[]'::jsonb,
  tags jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE llm_answers (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question text NOT NULL,
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  answer text NOT NULL,
  citations jsonb NOT NULL DEFAULT '[]'::jsonb,
  model text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX llm_answers_created_idx ON llm_answers (created_at DESC);

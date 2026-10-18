-- 中文全文检索用 zhparser。不安装 pgvector。
-- 第二期如果要做语义检索，另建 article_embeddings(article_id) 表，不要往 articles 上加向量列。

CREATE EXTENSION IF NOT EXISTS zhparser;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese') THEN
    CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
    ALTER TEXT SEARCH CONFIGURATION chinese
      ADD MAPPING FOR a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v,x,y,z
      WITH simple;
  END IF;
END
$$;

DO $$
BEGIN
  EXECUTE format(
    'ALTER DATABASE %I SET zhparser.punctuation_ignore = on', current_database()
  );
  EXECUTE format(
    'ALTER DATABASE %I SET zhparser.seg_with_duality = on', current_database()
  );
  EXECUTE format(
    'ALTER DATABASE %I SET zhparser.multi_short = on', current_database()
  );
END
$$;

SELECT sync_zhprs_custom_word();

CREATE TABLE sources (
  id text PRIMARY KEY,
  name text NOT NULL,
  author text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('wechat_mp', 'zsxq', 'feishu', 'markdown')),
  enabled boolean NOT NULL DEFAULT false,
  description text NOT NULL DEFAULT ''
);

CREATE TABLE articles (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id text NOT NULL REFERENCES sources (id),
  external_id text NOT NULL,
  content_hash text NOT NULL,
  title text NOT NULL,
  published_at timestamptz,
  body_md text NOT NULL,
  body_text text NOT NULL,
  search_tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('chinese', coalesce(title, '')), 'A')
    || setweight(to_tsvector('chinese', coalesce(body_text, '')), 'B')
  ) STORED,
  raw_meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_id, external_id)
);

CREATE INDEX articles_search_tsv_idx ON articles USING gin (search_tsv);
CREATE INDEX articles_source_published_idx
  ON articles (source_id, published_at DESC NULLS LAST, id DESC);
CREATE INDEX articles_published_idx
  ON articles (published_at DESC NULLS LAST, id DESC);

CREATE TABLE article_images (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  source_id text NOT NULL,
  original_ref text NOT NULL,
  storage_path text NOT NULL,
  content_hash text NOT NULL,
  UNIQUE (article_id, original_ref)
);

CREATE INDEX article_images_article_idx ON article_images (article_id);

CREATE TABLE companies (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticker text,
  name_zh text NOT NULL UNIQUE,
  name_en text NOT NULL DEFAULT '',
  market text NOT NULL DEFAULT '',
  aliases text[] NOT NULL DEFAULT '{}',
  seeded boolean NOT NULL DEFAULT true
);

CREATE UNIQUE INDEX companies_ticker_uidx
  ON companies (ticker) WHERE ticker IS NOT NULL AND ticker <> '';

CREATE TABLE article_mentions (
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  company_id bigint NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
  depth text NOT NULL CHECK (depth IN ('title', 'whole', 'section', 'passing')),
  hit_count integer NOT NULL CHECK (hit_count > 0),
  PRIMARY KEY (article_id, company_id)
);

CREATE INDEX article_mentions_company_idx ON article_mentions (company_id, depth);
CREATE INDEX article_mentions_article_idx ON article_mentions (article_id);

COMMENT ON TABLE articles IS
  'Archive text. pgvector is not installed; future embeddings belong in their own table.';

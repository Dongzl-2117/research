-- 中文译文进检索。译文切成段落块，和原文的块放在同一张表里：lang = 'zh'，块号从 10000 起。
-- anchor 是对照页上那一段的锚点（p-12），引用能直接跳过去。
-- 译文的标题和正文另建全文索引，文章级的召回和计数也能用中文命中英文文章。

ALTER TABLE article_chunks ADD COLUMN IF NOT EXISTS lang text NOT NULL DEFAULT '';
ALTER TABLE article_chunks ADD COLUMN IF NOT EXISTS anchor text NOT NULL DEFAULT '';

ALTER TABLE article_translations ADD COLUMN IF NOT EXISTS search_tsv tsvector GENERATED ALWAYS AS (
  setweight(to_tsvector('chinese', coalesce(title_zh, '')), 'A')
  || setweight(to_tsvector('chinese', coalesce(body_text, '')), 'B')
) STORED;

CREATE INDEX IF NOT EXISTS article_translations_search_tsv_idx ON article_translations USING gin (search_tsv);

-- 标题单独建全文索引。检索的「标题命中」一路只看标题，排序不再对整篇的 tsvector 逐篇算 ts_rank：
-- 常见词能命中几万篇，逐篇解压整篇的 tsvector 很慢，库越大越明显。
-- 这几个分词参数和 db.SEARCH_SETTINGS 一致，生成列在这条连接里算，结果和以后写入时一样。
SET zhparser.punctuation_ignore = on;
SET zhparser.seg_with_duality = on;
SET zhparser.multi_short = on;

ALTER TABLE articles ADD COLUMN IF NOT EXISTS title_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('chinese', coalesce(title, ''))) STORED;
CREATE INDEX IF NOT EXISTS articles_title_tsv_idx ON articles USING gin (title_tsv);

ALTER TABLE article_translations ADD COLUMN IF NOT EXISTS title_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('chinese', coalesce(title_zh, ''))) STORED;
CREATE INDEX IF NOT EXISTS article_translations_title_tsv_idx ON article_translations USING gin (title_tsv);

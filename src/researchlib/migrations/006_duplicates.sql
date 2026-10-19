-- 同一篇从两条路进来时只留一个主版本：AIHOT 导入的 id 是 aihot:<原 id>，文件归档用路径或 topic_id，
-- 同一篇两边各导一次就是两行；同一位作者在公众号和知识星球之间转发也是。
-- duplicate_of 指向主版本。重复的那一行保留原文，但不进列表、公司提及和检索，段落块和提及也不留。
-- article_fingerprints 存每篇正文里哈希最小的若干句，用来找近似重复；fingerprint_of 记算指纹时的 content_hash。

ALTER TABLE articles ADD COLUMN IF NOT EXISTS duplicate_of bigint REFERENCES articles (id) ON DELETE SET NULL;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS fingerprint_of text;
-- 认错了时用 research-dedupe --separate <id> 标成独立的一篇，以后不再参与认重复。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS keep_separate boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS articles_duplicate_of_idx ON articles (duplicate_of) WHERE duplicate_of IS NOT NULL;

CREATE TABLE IF NOT EXISTS article_fingerprints (
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  h bigint NOT NULL,
  PRIMARY KEY (article_id, h)
);

CREATE INDEX IF NOT EXISTS article_fingerprints_h_idx ON article_fingerprints (h);

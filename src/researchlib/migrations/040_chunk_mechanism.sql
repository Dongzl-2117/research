-- 检索块「讲没讲机制和原因」的分（0、1、2），由 research-mechanism 用模型打，搜索时只查表（researchlib/mechanism.py）。
-- 只建空表，不回填、不排队：打分花模型钱，Zhilin 同意以后在 Mac mini 上手动跑。
-- chunk_hash 是打分时这一块标题加正文的 md5，块重切以后对不上就算过期、下次重打。
CREATE TABLE IF NOT EXISTS chunk_mechanism (
  article_id bigint NOT NULL,
  ord integer NOT NULL,
  chunk_hash text NOT NULL,
  score smallint NOT NULL CHECK (score BETWEEN 0 AND 2),
  prompt_version integer NOT NULL,
  model text NOT NULL DEFAULT '',
  scored_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (article_id, ord),
  FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
);

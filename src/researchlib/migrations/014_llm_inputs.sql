-- 长文：以前摘要只给模型正文前 12000 字，观点只给前 30000 字。超过的标成 head_only，
-- 按新的取法（摘要读全文或开头结尾加各节开头，观点读提到这些公司的段落）重写。
ALTER TABLE article_digests ADD COLUMN IF NOT EXISTS head_only boolean NOT NULL DEFAULT false;
UPDATE article_digests d SET head_only = true FROM articles a WHERE a.id = d.article_id AND length(a.body_text) > 12000;

ALTER TABLE company_views ADD COLUMN IF NOT EXISTS head_only boolean NOT NULL DEFAULT false;
UPDATE company_views v SET head_only = true FROM articles a WHERE a.id = v.article_id AND length(a.body_text) > 30000;

-- 摘要、观点、论点失败的记次数：失败几次就隔几天再试（最多一周），不在每次打开页面时重花一遍。
CREATE TABLE IF NOT EXISTS llm_failures (
  kind text NOT NULL,
  item_id bigint NOT NULL,
  failures integer NOT NULL DEFAULT 1,
  last_error text NOT NULL DEFAULT '',
  last_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, item_id)
);

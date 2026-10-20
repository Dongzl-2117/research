-- 公司页的观点时间线：持仓和观察公司被深入谈到的文章，每篇抽一条作者对这家公司的判断。
-- judgment 为空表示这篇没对这家公司下判断（也记一行，免得反复去问模型）。
-- 风险、推翻条件、验证指标只在原文谈到时才有，没有就是空列表。
-- quote 是逐字核对过的原句，chunk_ord 是它所在的段落块；核对不上就留空。
-- content_hash 或 prompt_version 对不上就重抽。

CREATE TABLE IF NOT EXISTS company_views (
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  company_id bigint NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
  content_hash text NOT NULL,
  prompt_version integer NOT NULL,
  model text NOT NULL DEFAULT '',
  judgment text NOT NULL DEFAULT '',
  mechanism text NOT NULL DEFAULT '',
  stance text NOT NULL DEFAULT '' CHECK (stance IN ('', 'positive', 'negative', 'neutral', 'mixed')),
  risks jsonb NOT NULL DEFAULT '[]'::jsonb,
  falsifiers jsonb NOT NULL DEFAULT '[]'::jsonb,
  metrics jsonb NOT NULL DEFAULT '[]'::jsonb,
  quote text NOT NULL DEFAULT '',
  chunk_ord integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (article_id, company_id)
);

CREATE INDEX IF NOT EXISTS company_views_company_idx ON company_views (company_id);

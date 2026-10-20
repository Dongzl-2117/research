-- 持仓和观察公司的论点地图：把这家公司的观点归成几条论点，每条引用具体的观点（文章 id），
-- 支持的和反对的分开。views_hash 是这次用到的观点集合的指纹，观点有变化才重算。

CREATE TABLE IF NOT EXISTS company_theses (
  company_id bigint PRIMARY KEY REFERENCES companies (id) ON DELETE CASCADE,
  views_hash text NOT NULL,
  prompt_version integer NOT NULL,
  model text NOT NULL DEFAULT '',
  theses jsonb NOT NULL DEFAULT '[]'::jsonb,
  views_used integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);

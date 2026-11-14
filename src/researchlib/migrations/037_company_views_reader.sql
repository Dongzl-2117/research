-- 观点按哪一版读法读的正文（views.READER）。1：长文选中的段落按 12000 字一块装，装满 6 块就不再读，
-- 7 万字上下的长文常常丢了后三分之一（结尾的结论也在里面），行里却和读全了的一样。2：选中的段落全部读到。
-- 已有的行都记成 1；research-views --truncated 只从 1 里按旧读法逐篇算出真丢了段落的重抽，不整库重抽。
-- 只加一列、默认值是常量，Postgres 不重写表，一瞬间。重复跑没有影响。
ALTER TABLE company_views ADD COLUMN IF NOT EXISTS reader smallint NOT NULL DEFAULT 1;

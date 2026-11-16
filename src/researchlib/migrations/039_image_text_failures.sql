-- 识图失败的次数和时刻（审核 C3）：记成 :error 的图以前每晚重认、没有上限，坏图一直占着夜间名额、一直花钱。
-- 现在按次数退避（第 1 次失败后隔 20 小时，第 2 次后隔 3 天），第 3 次改记 :invalid，只能 --redo --article 点名重认。
-- 只加两列，不改已有的行：已有的 :error 行 failures 是 0、failed_at 为空，下一次照常重认。能重复跑。
ALTER TABLE image_texts ADD COLUMN IF NOT EXISTS failures smallint NOT NULL DEFAULT 0;
ALTER TABLE image_texts ADD COLUMN IF NOT EXISTS failed_at timestamptz;

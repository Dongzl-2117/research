-- 回答工作者只可更新自己领取的轮次；旧记录保留，常量默认值不做正文回填。
ALTER TABLE llm_answers ADD COLUMN IF NOT EXISTS run_token text NOT NULL DEFAULT '';
ALTER TABLE llm_answers ADD COLUMN IF NOT EXISTS worker_claimed boolean NOT NULL DEFAULT false;

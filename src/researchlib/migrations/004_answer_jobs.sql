-- 问库改成后台整合：提交后先存检索到的原文段落（status = running），模型在后台写回答，页面轮询状态。
-- 这样不在一个请求里等模型，Cloudflare 的请求超时（大约 100 秒）不会把回答截断。
-- 已有的回答都是写完的，默认 done。terms 存这次检索用到的词，回答页用来高亮原文。

ALTER TABLE llm_answers ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'done';
ALTER TABLE llm_answers ADD COLUMN IF NOT EXISTS error text NOT NULL DEFAULT '';
ALTER TABLE llm_answers ADD COLUMN IF NOT EXISTS terms jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE llm_answers ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE llm_answers DROP CONSTRAINT IF EXISTS llm_answers_status_check;
ALTER TABLE llm_answers ADD CONSTRAINT llm_answers_status_check CHECK (status IN ('running', 'done', 'error'));

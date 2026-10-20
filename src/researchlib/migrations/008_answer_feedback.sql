-- 回答页上的标注：哪段原文不相关、漏了哪篇。存成真值，research-eval-feedback 用这些真实问题回放检索。
-- question 和 scope 抄一份，回答删了也能回放。stats 记整合各步的耗时、批数和模型给的同义词。

CREATE TABLE IF NOT EXISTS answer_feedback (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  answer_id bigint REFERENCES llm_answers (id) ON DELETE SET NULL,
  question text NOT NULL,
  scope jsonb NOT NULL DEFAULT '{}'::jsonb,
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  chunk_ord integer,
  label text NOT NULL CHECK (label IN ('irrelevant', 'missing')),
  note text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS answer_feedback_once
  ON answer_feedback (answer_id, article_id, coalesce(chunk_ord, -1), label);

ALTER TABLE llm_answers ADD COLUMN IF NOT EXISTS stats jsonb NOT NULL DEFAULT '{}'::jsonb;

-- 只加字段和空表，不回写旧译文、不入付费队列。旧 provenance={} 表示来源对应尚未验证。
ALTER TABLE article_translations
  ADD COLUMN IF NOT EXISTS provenance jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS translation_attempts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  status text NOT NULL CHECK (status IN ('rejected', 'failed')),
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

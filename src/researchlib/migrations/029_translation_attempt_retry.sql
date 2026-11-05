-- 翻译失败的冷却。只加可空列，不回填、不改已有译文。
-- 旧行的 alignment_version 为空，不等于当前检查版本，因此不会挡住待译队列。
ALTER TABLE translation_attempts
  ADD COLUMN IF NOT EXISTS content_hash text,
  ADD COLUMN IF NOT EXISTS alignment_version integer;

CREATE INDEX IF NOT EXISTS translation_attempts_retry_idx
  ON translation_attempts (article_id, status, alignment_version, content_hash, created_at DESC);

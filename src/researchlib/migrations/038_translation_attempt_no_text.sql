-- 译制尝试多一种 no-text：只有图、没有要译的字的英文帖。记一笔，待译队列不再把它排在最前面占名额（审核 T7）。
-- 只换 status 的取值检查，不动已有的行；能重复跑。
ALTER TABLE translation_attempts DROP CONSTRAINT IF EXISTS translation_attempts_status_check;
ALTER TABLE translation_attempts
  ADD CONSTRAINT translation_attempts_status_check CHECK (status IN ('rejected', 'failed', 'no-text'));

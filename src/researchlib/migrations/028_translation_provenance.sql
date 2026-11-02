-- 只增加可空元数据；不改旧稿 complete/hash，不回填正文，不触发重译。
ALTER TABLE article_translations ADD COLUMN IF NOT EXISTS provenance jsonb;
COMMENT ON COLUMN article_translations.provenance IS
  'Ordered source/target identities and processing versions; NULL means legacy unverified, never an automatic rerun.';

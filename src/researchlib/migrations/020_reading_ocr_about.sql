-- 图片里的字写在文章旁路，观点和摘要读得到，不必改原文 content_hash。
-- about：论证落在这家公司才是 true。列举、顺带提到的记 false，不进时间线和论点。
-- angles：同一篇里对同一家公司的其他角度。
-- ocr_hash：识别结果变了，空白观点和几乎没正文的摘要才重抽。

ALTER TABLE articles ADD COLUMN IF NOT EXISTS ocr_text text NOT NULL DEFAULT '';
ALTER TABLE articles ADD COLUMN IF NOT EXISTS ocr_hash text NOT NULL DEFAULT '';

ALTER TABLE company_views ADD COLUMN IF NOT EXISTS about boolean NOT NULL DEFAULT true;
ALTER TABLE company_views ADD COLUMN IF NOT EXISTS ocr_hash text NOT NULL DEFAULT '';
ALTER TABLE company_views ADD COLUMN IF NOT EXISTS angles jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE article_digests ADD COLUMN IF NOT EXISTS ocr_hash text NOT NULL DEFAULT '';

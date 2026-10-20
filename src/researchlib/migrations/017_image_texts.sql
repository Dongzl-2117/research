-- 图片里的字（OCR）。按图片内容哈希只识别一次，多篇引用同一张图不重复识别；识别不出字也记一行。
-- 识别出的字切成段落块进检索：article_chunks.lang = 'ocr'，块号从 20000 起，anchor 指向文章页上那张图。

CREATE TABLE IF NOT EXISTS image_texts (
  content_hash text PRIMARY KEY,
  engine text NOT NULL,
  text text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

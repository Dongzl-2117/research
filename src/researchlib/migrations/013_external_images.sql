-- 正文里的外链图片（AIHOT 导入的 HTML、Markdown 里的 http 图）下载到本地，和归档里的图一样显示、一样进备份。
-- remote = true 的是下载来的图。导入时同步本地图片只动 remote = false 的行，下载来的不受影响。
-- image_fetches 记每个地址最近一次下载的结果：失败的隔一阵再试，不反复去打同一个坏地址。

ALTER TABLE article_images ADD COLUMN IF NOT EXISTS remote boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS image_fetches (
  url text PRIMARY KEY,
  ok boolean NOT NULL,
  content_hash text NOT NULL DEFAULT '',
  storage_path text NOT NULL DEFAULT '',
  error text NOT NULL DEFAULT '',
  attempts integer NOT NULL DEFAULT 1,
  fetched_at timestamptz NOT NULL DEFAULT now()
);

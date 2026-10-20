-- 摘要提示词的版本。提示词改了，旧摘要照常显示，被看到或跑 research-digest 时按新版重写。
-- 已有的摘要都是第 1 版。

ALTER TABLE article_digests ADD COLUMN IF NOT EXISTS prompt_version integer NOT NULL DEFAULT 1;

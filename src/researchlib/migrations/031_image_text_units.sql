-- 识图交回结构（researchlib.ocr_units）：谁说的、是不是截图主人、引用的哪条、哪条被图片边缘截断。
-- units 为空的是以前的自由文字，仍按旧规则整理；新识的图才有。
-- usage 是识这张图时接口报的 token 用量，跑过一晚以后按它算每张图实际花多少，再决定要不要重识旧图。
-- 两列都只加不填，现有的行不改，加列不重写表。
ALTER TABLE image_texts ADD COLUMN IF NOT EXISTS units jsonb;
ALTER TABLE image_texts ADD COLUMN IF NOT EXISTS usage jsonb;

-- 来源作者在平台上的其他显示名（config/sources.yaml 的 poster_names），启动时由 sync_sources 写入。
-- 识图排版时，截图里显示名是作者本人账号的消息写成作者（researchlib.ocr.article_owners）。
-- 只有 8 行左右，带默认值加列不重写表。
ALTER TABLE sources ADD COLUMN IF NOT EXISTS poster_names text[] NOT NULL DEFAULT '{}';

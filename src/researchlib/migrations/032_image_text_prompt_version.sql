-- 这张图是按第几版识图说明认的（researchlib.ocr.VISION_PROMPT_VERSION）。为空的是以前的说明认的。
-- 全库 research-ocr --redo 只重认版本比现在旧的图：停了再开从没认完的接着认，认过的不再付一次钱。
ALTER TABLE image_texts ADD COLUMN IF NOT EXISTS prompt_version smallint;

-- 识图交回结构（031）以后认的行是第 1 版：带结构、带用量、或者结构读不出来的，以前的说明都写不出这几样。
-- 只填还空着的行，重复跑不改已经填过的；表里约 2 万行。
UPDATE image_texts SET prompt_version = 1
WHERE prompt_version IS NULL
  AND (units IS NOT NULL OR usage IS NOT NULL OR right(engine, 8) = ':invalid');

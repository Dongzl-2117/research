-- 有歧义的别名的规则：「苹果」可能是水果，「中美的」里有「美的」，「中国银行业」里有「中国银行」。
-- {"needs": {"别名": ["佐证词", ...]}, "exclude": ["正则", ...]}
-- needs：这个别名只有在同一篇里还出现了佐证词（或这家公司的其他别名）时才算；
-- exclude：落在这些写法里的命中不算。规则进公司表指纹，改了启动时会在后台重算提及。

ALTER TABLE companies ADD COLUMN IF NOT EXISTS alias_rules jsonb NOT NULL DEFAULT '{}'::jsonb;

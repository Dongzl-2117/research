-- 检索、计数、问库的 SQL 都带着「可见文章」的条件 coalesce(raw_meta->>'hidden', '') = ''（visibility.visible_sql）。
-- jsonb 里取出来的值没有统计信息，规划器按默认的 0.5% 估：一万多篇只估出几十篇，于是召回段落时选逐篇的嵌套循环。
-- 这个计划好坏看运气：拿线上公开接口搭的一万篇本地副本上，另加 5000 篇隐藏文章（线上有星球读者帖、短帖）时
-- 召回那一条 SQL 两三百毫秒；不加时同一条 7 到 10 秒（每篇文章各查一遍全文索引）。补上这个表达式的统计以后
-- 估得准，换成哈希连接，几十毫秒，隐藏文章多少都一样。
-- 只加统计信息，不改数据；ANALYZE 一万多篇一秒内。以后 autovacuum 照常更新它。重复跑没有影响。
CREATE STATISTICS IF NOT EXISTS articles_hidden_stats ON (coalesce(raw_meta->>'hidden', '')) FROM articles;
ANALYZE articles;

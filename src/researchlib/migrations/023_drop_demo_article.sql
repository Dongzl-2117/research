-- 10 月 19 日手工插进乘风破浪的一篇测试文章「Aggregation Theory (demo)」，线上能看到。删掉。
-- 引用 articles 的外键都是 ON DELETE CASCADE 或 SET NULL，块、提及、摘要跟着删。

DELETE FROM articles WHERE source_id = 'chengfengpolang' AND title = 'Aggregation Theory (demo)';

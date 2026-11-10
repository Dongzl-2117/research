-- 归档自己的说明文件不是文章，以前却当成文章导进了库，网站、公司页、检索和问库都看得到：
-- 方伟公众号归档根上的 README.md（抓取说明）和 INDEX.md（文章目录，挂了 30 家公司，线上 1、2、3），
-- 乘风破浪星球导出的话题索引（线上 803）。导入从此不收（ingest/archive.is_archive_note），这里删掉库里已有的。
-- 按篇的来源（公众号、星球、雪球、Stratechery）都删；飞书这类文档树不动：那里 index.md 可能是一层目录自己的那一页。
-- 认文件名（raw_meta.filename），没记文件名的认外部 id 的最后一段（星球的是去掉扩展名的 INDEX）。
-- 引用 articles 的外键都是 ON DELETE CASCADE 或 SET NULL：块、向量、提及、摘要跟着删。再跑一遍结果一样。
DELETE FROM articles a
USING sources s
WHERE s.id = a.source_id
  AND s.kind <> 'feishu'
  AND (
    lower(regexp_replace(coalesce(a.raw_meta->>'filename', ''), '^.*/', ''))
      IN ('readme.md', 'readme.markdown', 'index.md', 'index.markdown')
    OR lower(regexp_replace(a.external_id, '^.*/', ''))
      IN ('readme', 'readme.md', 'readme.markdown', 'index', 'index.md', 'index.markdown')
  );

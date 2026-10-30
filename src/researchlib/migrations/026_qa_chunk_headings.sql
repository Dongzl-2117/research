-- 知识星球问答导出的小标题「提问（网友 → 方伟Rey）」「回答（方伟Rey）」原样当了段落块的章节名：
-- 检索结果的「§ 提问（某某 → 方伟Rey）」、问库的引用和接口里都带着网友的昵称。新切的块在 make_chunks 里
-- 只留「提问」「回答」，这里改掉已入库的。和阅读页一样，正文里提问、回答两种小标题都有才改。
-- 只改章节名，块正文、文章、摘要都不动。章节名也进向量，夜间的 research-embed 按 text_hash 重算这些块。

UPDATE article_chunks c
SET heading = CASE WHEN c.heading LIKE '提问%' THEN '提问' ELSE '回答' END
WHERE c.heading ~ '^(提问|回答)\s*[（(].+[）)]\s*$'
  AND EXISTS (
    SELECT 1
    FROM articles a
    WHERE a.id = c.article_id
      AND a.body_md ~ '(?n)^[ \t]{0,3}#{1,6}[ \t]+提问\s*[（(].+[）)]\s*$'
      AND a.body_md ~ '(?n)^[ \t]{0,3}#{1,6}[ \t]+回答\s*[（(].+[）)]\s*$'
  );

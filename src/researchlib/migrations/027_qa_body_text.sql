-- 星球问答帖的检索文字（articles.body_text）留着导出的小标题「提问（网友 → 方伟Rey）」「回答（方伟Rey）」，
-- 括号里是提问网友的昵称。接口列表的摘录、没有摘要时卡片和按天页的摘录都从这里取，昵称就露在网站上。
-- 新入库和 research-clean 重算的只留「提问」「回答」（boilerplate.search_text），这里改掉已入库的；
-- 改动记录（article_revisions）里存的旧版检索文字一起改，不然「改动」页会把这一行当成改过的，昵称从对比里露出来。
-- 和 026 一样，原文里提问、回答两种小标题都有才改。只改这两行，别的字不动；全文索引随 body_text 重算，向量不涉及。

UPDATE articles
SET body_text = regexp_replace(
  regexp_replace(body_text, '(?n)^[ \t]*提问[ \t]*[（(].+[）)][ \t]*$', '提问', 'g'),
  '(?n)^[ \t]*回答[ \t]*[（(].+[）)][ \t]*$', '回答', 'g'
)
WHERE body_text ~ '(?n)^[ \t]*(提问|回答)[ \t]*[（(].+[）)][ \t]*$'
  AND body_md ~ '(?n)^[ \t]{0,3}#{1,6}[ \t]+提问[ \t]*[（(].+[）)][ \t]*$'
  AND body_md ~ '(?n)^[ \t]{0,3}#{1,6}[ \t]+回答[ \t]*[（(].+[）)][ \t]*$';

UPDATE article_revisions
SET body_text = regexp_replace(
  regexp_replace(body_text, '(?n)^[ \t]*提问[ \t]*[（(].+[）)][ \t]*$', '提问', 'g'),
  '(?n)^[ \t]*回答[ \t]*[（(].+[）)][ \t]*$', '回答', 'g'
)
WHERE body_text ~ '(?n)^[ \t]*(提问|回答)[ \t]*[（(].+[）)][ \t]*$'
  AND body_md ~ '(?n)^[ \t]{0,3}#{1,6}[ \t]+提问[ \t]*[（(].+[）)][ \t]*$'
  AND body_md ~ '(?n)^[ \t]{0,3}#{1,6}[ \t]+回答[ \t]*[（(].+[）)][ \t]*$';

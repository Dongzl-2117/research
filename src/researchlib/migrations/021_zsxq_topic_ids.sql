-- 知识星球的话题号写进 raw_meta.topic_id，去重按「同一来源、同一话题号」认同一篇。
-- 10 月 20 日以前导入的帖子没读到 raw/ 里的话题 JSON，外部 id 是文件名，raw_meta 里也没有话题号；
-- 后来按话题号又导入了一遍，同一帖成了两行。话题号在正文开头的归档头里（「- topic_id：`…`」）。
-- 只改 raw_meta，不动 content_hash，摘要和观点不用重抽。

UPDATE articles a
SET raw_meta = a.raw_meta || jsonb_build_object('topic_id', found.topic_id)
FROM (
  SELECT x.id, substring(left(x.body_md, 2000) FROM 'topic_id\s*[：:]\s*`?([0-9]{5,})') AS topic_id
  FROM articles x
  JOIN sources s ON s.id = x.source_id
  WHERE s.kind = 'zsxq' AND NOT (x.raw_meta ? 'topic_id')
) found
WHERE a.id = found.id AND found.topic_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS articles_topic_id_idx ON articles (source_id, (raw_meta->>'topic_id'));

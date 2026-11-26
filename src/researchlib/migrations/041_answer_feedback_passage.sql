-- 标注跟着读者看到的段落走。旧回答在标注后更新过，或已删除时，不用现在的文字补猜旧出处。
ALTER TABLE answer_feedback ADD COLUMN IF NOT EXISTS passage_hash text;

UPDATE answer_feedback f
SET passage_hash = md5(concat_ws(E'\n', coalesce(c ->> 'lang', ''), coalesce(c ->> 'heading', ''), c ->> 'text'))
FROM llm_answers a CROSS JOIN LATERAL jsonb_array_elements(a.citations) c
WHERE f.answer_id = a.id AND f.label = 'irrelevant' AND f.passage_hash IS NULL
  AND a.updated_at <= f.created_at AND c ->> 'article_id' = f.article_id::text
  AND c ->> 'ord' = f.chunk_ord::text AND c ->> 'text' <> '';

-- 同一回答重新整合时，原块号可能已经换了内容。新旧判断各自保存，补漏仍按原来的键去重。
DROP INDEX IF EXISTS answer_feedback_once;
CREATE UNIQUE INDEX answer_feedback_once
  ON answer_feedback (answer_id, article_id, coalesce(chunk_ord, -1), label, coalesce(passage_hash, ''));

-- 飞书文库的目录树。父节点和次序从正文里的相对链接推断，不接飞书 wiki。
-- 一篇第一次被谁链接到，谁就是 parent；交叉链接不记在这里。
-- orphan：从根广度优先走不到的篇，仍留在表里，页面上单独一节。

CREATE TABLE IF NOT EXISTS doc_tree (
  source_id text NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
  article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
  parent_id bigint REFERENCES articles (id) ON DELETE SET NULL,
  ord integer NOT NULL,
  orphan boolean NOT NULL DEFAULT false,
  PRIMARY KEY (source_id, article_id),
  CHECK (parent_id IS NULL OR parent_id <> article_id)
);

CREATE INDEX IF NOT EXISTS doc_tree_parent_idx
  ON doc_tree (source_id, parent_id, ord);

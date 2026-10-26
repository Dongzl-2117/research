-- feishu2md 把飞书的高亮块导成 GitHub 提示框：引用块第一行写「[!NOTE]」。转成纯文本时标记留在了正文里，
-- 卡片导读按「!」断句，开头剩下「NOTE]」。新导入的在 markdown_to_text 里去掉，这里清掉已入库的。
-- 只改 body_text，不动 body_md 和 content_hash，摘要和观点不用重抽。

UPDATE articles
SET body_text = regexp_replace(
  regexp_replace(body_text, '^[ \t]*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(\n|$)', '', 'gin'),
  '^([ \t]*)\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*', '\1', 'gin'
)
WHERE body_text ~* '\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]';

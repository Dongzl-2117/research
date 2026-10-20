-- 去重核对页按「最近认出」排：标成重复时记下时间，放开时清掉。
ALTER TABLE articles ADD COLUMN IF NOT EXISTS duplicate_marked_at timestamptz;

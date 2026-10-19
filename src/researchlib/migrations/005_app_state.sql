-- 进程自己记的少量状态。现在只有一项：上次重算公司提及时公司表的指纹。
-- 公司别名变了（比如新加了泡泡玛特），启动时发现指纹对不上，就在后台重算一遍提及。

CREATE TABLE IF NOT EXISTS app_state (
  key text PRIMARY KEY,
  value text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

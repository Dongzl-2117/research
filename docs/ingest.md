# 导入

导入只接受 `config/sources.yaml` 里 `enabled: true` 的来源。停用的来源、不认识的 id、用错类型的命令都会直接拒绝，退出码是 2，数据库不会写。

以后要启用占位来源，把对应项的 `enabled` 改成 `true` 即可。类型已经配好：

| id | 类型 | 命令 |
| --- | --- | --- |
| fangwei-mp | wechat_mp | `research-ingest-wechat` |
| fangwei-zsxq | zsxq | `research-ingest-zsxq` |
| chengfengpolang | zsxq | `research-ingest-zsxq` |
| gudujinhuati | markdown | `research-ingest-markdown` |
| linwenfeng-mp | wechat_mp | `research-ingest-wechat` |
| linwenfeng-feishu | feishu | `research-ingest-feishu` |

`linwenfeng-feishu` 保持启用，用 `research-ingest-feishu` 导入。`linwenfeng-mp` 停用，公众号不入库。

幂等键是 `(来源, 外部 id)`。外部 id 优先用 frontmatter 或知识星球 `topic_id`，否则用归档内的相对路径。同一篇再跑一遍不会复制；正文、标题或时间变了才更新。

发布日期按这个顺序取：Markdown frontmatter（`date`、`publish_time`、`create_time` 等）、知识星球 raw JSON 的 `create_time`（包括嵌套的 `resp_data.topic`）、正文里的公众号发布时间标记、文件名里的日期。已入库但 `published_at` 仍为空、而正文或 `raw_meta` 里认得出日期的，可以补一次：

```bash
research-backfill-dates
```

这条命令只填空日期，不改已经有日期的行。找不到日期的仍留空，首页放在「日期不详」。

公司别名改完后跑：

```bash
research-rebuild-mentions
```

手工改过的公司行如果还想保留，先 `UPDATE companies SET seeded = false WHERE name_zh = '...'`。`seeded = true` 的行会在进程启动时被种子数据覆盖。

## 在运行中的 compose 里导入

把归档放到 `./data/inbox`（这个目录被 git 忽略）：

```bash
docker compose exec app research-ingest-wechat \
  --source fangwei-mp \
  --input /data/inbox/fangwei-mp

docker compose exec app research-ingest-zsxq \
  --source fangwei-zsxq \
  --input /data/inbox/fangwei-zsxq

docker compose exec app research-ingest-zsxq \
  --source chengfengpolang \
  --input /data/inbox/chengfengpolang
```

也可以在宿主机上装好依赖，把 `DATABASE_URL` 指到 `127.0.0.1:5432` 后直接跑同样的命令。

## 微信公众号

接受一个目录或一个 zip。目录里可以同时有解压后的 Markdown 和 `01-text.zip`、`02-images-a.zip` 这种图片包。解析会：

- 跳过路径里带 `..` 的 zip 条目
- 再展开一层嵌套 zip
- 先按 Markdown 里的相对路径找图，找不到再按文件名在图片包里找
- 外链图片不下载，页面上显示「外部图片已省略」

正文用 UTF-8，失败时再试 GB18030。

## 知识星球

期望的目录：

```text
archive/
  md/*.md
  images/
  raw/*.json
```

`raw` 里的 JSON 只提供 `topic_id`、时间和标题。正文以 Markdown 为准。文件名里的长数字会拿去对 JSON。

## 飞书和通用 Markdown

相对路径就是外部 id，所以子文档放在子目录里再次导入仍然对得上。飞书和通用 Markdown 的文章页会列出同目录的其他篇。

## AIHOT（可选，只读）

这不是把 AIHOT 的新闻或日报流水线搬过来。只从它的 Postgres `articles` 表 `SELECT` 正文，按白名单写入本库。

```bash
export AIHOT_DATABASE_URL=postgresql://...
research-ingest-aihot --dry-run
research-ingest-aihot
```

连接会设成只读事务。列名和来源别名在 `config/aihot_import.yaml`。映射不上、或者映射到未启用的来源，计入「拒绝」，不写入。外部 id 形如 `aihot:<原 id>`，和文件归档的 id 分开，避免两边撞车。

不要把 `AIHOT_DATABASE_URL` 指回本库。

## 计数报告

每次导入打印：

- 归档文章：扫到的 Markdown 篇数
- 数据库文章：这个来源当前的行数
- 写入：新增、更新、未变
- 拒绝：没通过白名单的行（文件导入通常是 0，AIHOT 会有）
- 归档图片、新保存图片、挂上文章的图片、缺失图片引用

图片按内容哈希存在 `data/images/<来源>/<哈希>`。同一张图不会存两份。

## 示例数据

```bash
ALLOW_SAMPLE_SEED=1 research-seed-sample
```

示例文章标题带「示例」，页面顶部会提示这不是付费原文。生产库不要开 `ALLOW_SAMPLE_SEED`。

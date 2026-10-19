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

`linwenfeng-feishu` 保持启用，用 `research-ingest-feishu` 导入，但 `timeline: false`，不进首页按天和日汇总。作者、公司、检索、问库仍能找到它。`linwenfeng-mp` 停用，公众号不入库。雪球先不加来源、不导入。

幂等键是 `(来源, 外部 id)`。外部 id 优先用 frontmatter 或知识星球 `topic_id`，否则用归档内的相对路径。同一篇再跑一遍不会复制；正文、标题或时间变了才更新。

## 同一篇只留一份

幂等键管不住两条路进来的同一篇：AIHOT 导入的外部 id 是 `aihot:<原 id>`，文件归档用路径或 `topic_id`，同一篇两边各导一次就是两行；同一位作者在公众号和知识星球之间转发也是。所以每篇写入后还会认一次重复：

- 正文按句切开，只留汉字、字母和数字，每句算一个哈希，每篇存哈希最小的 32 句当指纹（`article_fingerprints`）。
- 只和同一位作者、发布时间前后一周内的文章比。指纹重合够多的，再逐句核对：较短那篇至少 80% 的句子（且至少 3 句）出现在另一篇里，两篇长度也差不多，才算同一篇。同一位作者很多篇都有的句子（页脚、免责声明）不算。
- 主版本：图片多的；明显更全的（另一份不到它的四分之三，比如半截稿）；发得早的；先入库的。只差一行归档头的两份，先入库的那份留着。
- 其余的 `duplicate_of` 指向主版本，段落块、公司提及和观点删掉，不进首页、作者页、公司页、检索和问库。文章页还能打开，署名行写「同一篇也在 某来源」。

导入报告多一行「重复」。第一次部署时，应用启动后在后台把全库认一遍，日志里有开始和完成两行；以后只认新导入或改过的。认错了：

```bash
docker compose exec app research-dedupe --separate 123   # 把这篇标成独立的一篇，以后不再参与认重复
docker compose exec app research-dedupe                  # 全库重认一遍
```

在 6000 篇的评测语料（大量套话句子反复出现，比真实文章重复得多）上，全库认一遍要一分多钟，误认 2 篇。

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
research-ingest-aihot              # 全读一遍
research-ingest-aihot --days 14    # 只读最近 14 天发布的（定时增量用）
```

连接会设成只读事务。列名和来源别名在 `config/aihot_import.yaml`。SQL 里只读映射到本库已启用来源的那些行，别的来源的正文不碰；其余的只数一下，计入「拒绝」。外部 id 形如 `aihot:<原 id>`。同一篇如果已经从文件归档导过，会认成重复（见上面），不会在列表里出现两次。

`--days` 只在发布时间那一列是时间类型时生效；不是的话照常全读。

不要把 `AIHOT_DATABASE_URL` 指回本库。

### 定时增量

`scripts/ingest-aihot.sh` 在容器里跑 `research-ingest-aihot --days 14`。用 launchd 每小时跑一次，见 [backup.md](backup.md)。先确认容器里连得上 AIHOT 的库：

```bash
docker compose exec app research-ingest-aihot --dry-run --days 14
```

每次导入（文件归档和 AIHOT 都算）都会在 `app_state` 里记下这个来源最近一次导入的时间和计数（`import:<来源>`）。首页按天的顶上，某个来源最近一篇离现在比它平时的间隔长很多（三倍以上，至少一周）时，提示一行，并带上最近一次导入的时间，导入停了也能看出来。最近不到 10 篇带日期的来源不判断。

## 计数报告

每次导入打印：

- 归档文章：扫到的 Markdown 篇数
- 数据库文章：这个来源当前的行数
- 写入：新增、更新、未变
- 拒绝：没通过白名单的行（文件导入通常是 0，AIHOT 会有）
- 重复：写入后认出是库里已有的同一篇
- 归档图片、新保存图片、挂上文章的图片、缺失图片引用

图片按内容哈希存在 `data/images/<来源>/<哈希>`。同一张图不会存两份。

## 示例数据

```bash
ALLOW_SAMPLE_SEED=1 research-seed-sample
```

示例文章标题带「示例」，页面顶部会提示这不是付费原文。生产库不要开 `ALLOW_SAMPLE_SEED`。

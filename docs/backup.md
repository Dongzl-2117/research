# 备份和定时任务

这个库的第一件事是保存全文。知识星球和公众号的原文随时可能被作者删掉，库里这份就是唯一的一份，所以要有备份，而且要定期确认备份真的恢复得出来。

全文、段落块、公司提及、摘要、观点、问库记录都在 Postgres 的 `pgdata` 卷里；图片在 `./data/images`。`config/` 下的配置在 git 里。`.env` 里有口令，不进备份，自己另存一份。

## 三个脚本

| 脚本 | 做什么 |
| --- | --- |
| `scripts/backup.sh` | `pg_dump` 导出一份（custom 格式），用 `pg_restore --list` 读一遍确认完整，再增量同步图片。导出保留 30 天（`KEEP_DAYS`）。导出前记下篇数，存在 `research-时间.dump.articles` 里 |
| `scripts/restore-check.sh` | 把最新一份导出恢复到临时库 `research_restore_check`，核对篇数不少于导出时，然后删掉临时库。不碰正式库 |
| `scripts/ingest-aihot.sh` | 从 AIHOT 只读导入最近 14 天（`DAYS`）发布的白名单文章，见 [ingest.md](ingest.md) |

都通过 `docker compose exec` 在容器里跑，不需要在宿主机装 Postgres 客户端。

```bash
BACKUP_DIR=/Volumes/备份盘/research scripts/backup.sh
BACKUP_DIR=/Volumes/备份盘/research scripts/restore-check.sh
```

`BACKUP_DIR` 放另一块盘或云盘同步目录（比如 iCloud Drive 里的一个文件夹），不要和仓库、Docker 卷在同一块盘上：盘坏了两边一起没。目录结构：

```text
BACKUP_DIR/
  db/research-20261020-031700.dump
  db/research-20261020-031700.dump.articles
  images/<来源>/<内容哈希>
  backup.log
  restore-check.log
```

图片按内容哈希存，同名就是同一张，同步只增不删，库里误删了的图在备份里还在。

## 用 launchd 定时跑

```bash
cd ~/research
BACKUP_DIR=/Volumes/备份盘/research scripts/install-launchd.sh
```

装三个当前用户的定时任务（模板在 `deploy/launchd/`）：

| 任务 | 时间 |
| --- | --- |
| `com.zldong.research.backup` | 每晚 03:17 |
| `com.zldong.research.restore-check` | 每月 1 号 04:23 |
| `com.zldong.research.ingest-aihot` | 每小时 |

日志在 `~/Library/Logs/research/`。Mac mini 睡着时错过的那一次，醒来后 launchd 会补跑。卸载：

```bash
scripts/install-launchd.sh --uninstall
```

## 真要恢复时

新库要先设好和正式库一样的分词参数，再 `pg_restore`：恢复时会重算全文索引，参数不一样，检索结果就和原来不一样。

```bash
docker compose up -d db
docker compose exec -T db dropdb -U research --if-exists research
docker compose exec -T db createdb -U research research
for s in punctuation_ignore seg_with_duality multi_short; do
  docker compose exec -T db psql -U research -d research -c "ALTER DATABASE research SET zhparser.$s = on"
done
docker compose exec -T db pg_restore -U research -d research --no-owner < /Volumes/备份盘/research/db/research-20261020-031700.dump
rsync -a /Volumes/备份盘/research/images/ ./data/images/
docker compose up -d
```

6000 篇的评测库导出约 10 秒、7.5MB，恢复约 45 秒（大部分时间在重算全文索引）。

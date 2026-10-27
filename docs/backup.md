# 备份和定时任务

这个库的第一件事是保存全文。知识星球和公众号的原文随时可能被作者删掉，库里这份就是唯一的一份，所以要有备份，而且要定期确认备份真的恢复得出来。

全文、段落块、公司提及、摘要、观点、问库记录都在 Postgres 的 `pgdata` 卷里；图片在 `./data/images`。`config/` 下的配置在 git 里。`.env` 里有口令，不进备份，自己另存一份。

## 定时脚本

| 脚本 | 做什么 |
| --- | --- |
| `scripts/backup.sh` | `pg_dump` 导出一份（custom 格式），用 `pg_restore --list` 读一遍确认完整，再增量同步图片。导出保留 30 天（`KEEP_DAYS`）。导出前记下篇数，存在 `research-时间.dump.articles` 里 |
| `scripts/restore-check.sh` | 把最新一份导出恢复到临时库 `research_restore_check`，核对篇数不少于导出时，然后删掉临时库。不碰正式库 |
| `scripts/crawl-fangwei-mp.sh` | 方伟公众号增量抓取（极致了接口，`DAJIALA_KEY` 取环境变量或仓库根 `.env`），导入（`--origin crawl`），再给新文章补摘要 |
| `scripts/crawl-fangwei-zsxq.sh` | 方伟知识星球增量抓取（`data/inbox/fangwei-zsxq/dl.py`，令牌在 `~/zsxq/zsxq.env`），导入（`--origin crawl`），再补摘要 |
| `scripts/ingest-aihot.sh` | （AIHOT 已下线，默认不装）从 AIHOT 只读导入最近 14 天（`DAYS`）发布的白名单文章，见 [ingest.md](ingest.md)；导完把新文章里的外链图片下载到本地（`IMAGES_LIMIT`，默认 200 张） |
| `scripts/nightly.sh` | 夜间补齐，见下面 |

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

## 夜间补齐

摘要、观点平时只在页面被打开时给当前卡片排队，没人打开的文章一直缺着；外链图片、论点也一样。`scripts/nightly.sh` 每晚各补一批，每一步有篇数上限，跑不完的下一晚接着补：

| 步骤 | 命令 | 上限 |
| --- | --- | --- |
| 外链图片 | `research-fetch-images` | `IMAGES_LIMIT`，默认 300 个地址 |
| 观点 | `research-views`（同时抽 `VIEWS_WORKERS` 篇，默认 3；持仓和观察的、中文的先排） | `VIEWS_LIMIT`，默认 60 篇 |
| 摘要 | `research-digest` | `DIGEST_LIMIT`，默认 80 篇 |
| 论点 | `research-theses --portfolio`（`THESES_SCOPE=all` 补全部，`off` 不跑） | 持仓和观察名单里观点够数的公司，观点变了才算，一家一天最多一次 |
| 图片文字 | `research-ocr`（`NIGHTLY_OCR=1` 时，见 [ocr.md](ocr.md)） | `OCR_LIMIT`，默认 400 张 |

观点、摘要、图片文字各自最多跑 `MINUTES` 分钟（默认 150）。某一步失败不影响后面的步骤。白天、部署和应用启动时都不跑这些。

## 出问题才提示

每个定时脚本跑完把结果写进 `data/status/<任务>.json`（成功也写）。首页按天的顶上只在出问题时提示一行，正常时什么都不显示：

| 任务 | 什么时候提示 |
| --- | --- |
| 备份 | 最近一次失败，或 36 小时没成功 |
| 试恢复 | 最近一次失败，或 40 天没成功 |
| 方伟公众号抓取、方伟星球抓取 | 最近一次失败，或 30 小时没成功（装了才看） |
| AIHOT 定时导入 | 最近一次失败，或 6 小时没成功（装了才看；默认不装） |
| 夜间补齐 | 有步骤失败，或 50 小时没成功（装了才看） |
| 自动上线 | 拉代码、重建失败，重启后应用没起来，CI 没过，仓库不在 main 或有没提交的改动，或 6 小时取不到 main、查不到 CI（装了才看） |
| 后台重算 | 应用启动后认重复、重算提及失败 |
| LLM | 凭证不可用（DeepSeek key 缺失，或 Grok 登录失效），或连续失败 3 次 |

提示里带失败的那一步和错误输出的最后一行，比如「备份失败（10-20 03:17）：导出数据库失败（退出码 1）：…」。从没跑过的任务（没有状态文件）不提示，所以装好定时任务后先手动跑一次备份，确认 `data/status/backup.json` 写出来了。`--uninstall` 会删掉定时抓取、定时导入、夜间补齐和自动上线的状态文件，免得卸了以后还提示「太久没跑」；备份的留着。

## 用 launchd 定时跑

```bash
cd ~/research
BACKUP_DIR=~/research-backups scripts/install-launchd.sh   # 同盘先顶用；云盘目录另定
# 默认装方伟公众号、方伟星球的定时抓取；不要：INSTALL_CRAWL=0
# 默认不装每小时 AIHOT 增量（AIHOT 已下线），以前装过的会顺手卸掉。要装：INSTALL_INGEST_AIHOT=1 BACKUP_DIR=… scripts/install-launchd.sh
# 不要夜间补齐：INSTALL_NIGHTLY=0；夜间也识别图片里的字：NIGHTLY_OCR=1
# 不要自动上线：INSTALL_DEPLOY=0
```

这些设置记在 `data/launchd.env`，以后重跑 `scripts/install-launchd.sh` 不用再写（命令行上设了的优先）。

装这些当前用户的定时任务（模板在 `deploy/launchd/`）：

| 任务 | 时间 |
| --- | --- |
| `com.zldong.research.nightly` | 每晚 00:37 |
| `com.zldong.research.backup` | 每晚 03:17 |
| `com.zldong.research.restore-check` | 每月 1 号 04:23 |
| `com.zldong.research.crawl-fangwei-mp` | 每天 08:10、20:10 |
| `com.zldong.research.crawl-fangwei-zsxq` | 每天 08:40、20:40 |
| `com.zldong.research.ingest-aihot` | 每小时（要装才有） |
| `com.zldong.research.deploy` | 每 10 分钟，装好时先跑一次（见 [deploy.md](deploy.md)「更新」） |

日志在 `~/Library/Logs/research/`。Mac mini 睡着时错过的那一次，醒来后 launchd 会补跑。卸载：

```bash
scripts/install-launchd.sh --uninstall
```

`BACKUP_DIR` 放在 iCloud Drive 或外接盘上时，launchd 跑的 `/bin/bash` 没有权限写那里，会报「Operation not permitted」（首页会提示备份失败）：在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里加上 `/bin/bash`。

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

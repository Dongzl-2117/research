# 部署

站点域名是 **investment.zldong.com**。仓库名仍然是 `Dongzl-2117/research`。

目标机器是 Mac mini（M4，16GB 内存，大约 228GB 磁盘）。服务用 docker compose 跑，Postgres 把 `shared_buffers` 限制在 256MB，避免和本机其他进程抢内存。

## 第一次启动

```bash
cp .env.example .env
docker compose up -d --build
```

应用监听容器内的 `8088`，compose 只把宿主机的 **127.0.0.1:8088** 转进去。不要改成 `0.0.0.0`。数据库端口同样只绑定 `127.0.0.1:5432`。

应用启动时会自己做迁移（按文件名顺序，到 `023_drop_demo_article.sql`）、同步 `config/sources.yaml`（星主的星球昵称 `poster_names` 改了，按新名单重算已入库帖子的原创标记）、写入公司种子，并给还没有段落块的文章（和有译文但还没有译文块的文章）补切。以前每篇最多切 400 块，正好切满 400 块的长文启动时重切一遍，后面的内容才进检索。上次没写完的问库整合标成中断，回答页可以重新整合。

`019` 只建飞书目录表。部署后若林文丰已经在库里，跑一次 `research-doc-tree --source linwenfeng-feishu`（不访问飞书）。方伟星球若要改成真实 `create_time`，跑 `research-retime`（见 [ingest.md](ingest.md)），它会按新的 `published_at` 重排观点时间线和「同期别人怎么看」。这两条都不要在部署时自动跑。

`015_title_search.sql` 给文章表和译文表各加一列标题的全文索引，要重写一遍表：几千篇几秒钟，期间应用还没起来。`014_llm_inputs.sql` 把以前只读了长文开头写出来的摘要和观点标出来，之后按新的读法慢慢重写（见 [llm.md](llm.md)），不在部署时跑。

之后在后台依次做两件事，不挡启动：

1. 认重复文章。第一次部署、或认法升了版本（`duplicates.VERSION`）时全库认一遍，指纹全部重算（日志「全库认一遍重复文章」「重复文章认完」），以后只补没算过指纹的。
2. 公司别名或歧义规则和上次重算提及时不一样，就重算全部文章的公司提及（日志「公司别名变了」「公司提及重算完成」）。

几千篇时两步加起来几分钟，期间页面照常能用，只是重复文章和提及还没更新完。

备份、试恢复、夜间补齐、定时抓取和自动上线用 launchd 跑，见 [backup.md](backup.md)。这几个任务出了问题写进 `data/status/`，首页不挂红条。

不会在启动或部署时跑 `research-digest`、`research-views`、`research-theses`。打开页面时只给当前卡片里缺的排队，其余由夜间任务每晚补一批，见 [llm.md](llm.md)。

## 语义检索（可选）

Docker 里用不了 Mac mini 的 GPU，向量模型跑在 macOS 本机：

```bash
brew install ollama
brew services start ollama
ollama pull bge-m3
```

`.env` 里加 `EMBEDDING_BASE_URL=http://host.docker.internal:11434/v1`（模型默认 `bge-m3`），重建并启动：

```bash
docker compose up -d --build        # db 镜像这次会装上 pgvector，启动时建向量表
docker compose exec app research-embed   # 第一次全库回填，之后夜间补齐每晚补新的
```

不配 `EMBEDDING_BASE_URL` 时一切照旧，只用关键词检索。原理和参数见 [retrieval.md](retrieval.md)「语义检索」。

看有没有用上：检索页搜一个带具体词的问题（比如「costco 商业模式」），语义这一路没用上或向量没算完时，结果上方有一句说明。`research-embed` 跑完会打印「库里 x/y 块有向量」，算不了而跳过的块列在最后。查询向量连不上时 app 日志里有「语义检索暂停」。

Ollama 闲置 5 分钟就卸下模型，之后的第一次搜索要等它重新加载（几秒，查询向量这时最多等 15 秒）。想让模型常驻（bge-m3 占一两 GB 内存）：

```bash
launchctl setenv OLLAMA_KEEP_ALIVE -1
brew services restart ollama
ollama ps        # 搜一次之后，UNTIL 一列是 Forever 就是常驻了
```

## 更新

合并进 main 的改动自己上线。Mac mini 上的定时任务 `com.zldong.research.deploy` 每 10 分钟看一次 main：有新提交，而且这个提交在 GitHub 上的 CI（Tests）通过了，就拉代码、`docker compose up -d --build`，等 `/health` 通了算上线（`scripts/deploy.sh`）。一般合并后 10 到 20 分钟上线。迁移和后台重算在应用启动时自己做；定时任务的模板改了，按上次安装的设置重装。

只要装一次：

```bash
cd ~/research
git pull
scripts/install-launchd.sh   # 以前装过备份的，BACKUP_DIR 从装好的备份任务里读；没装过的带上 BACKUP_DIR=…
```

装好时先跑一轮，不用再另外 `docker compose up -d --build`。日志在 `~/Library/Logs/research/deploy.log`。失败写 `data/status/`，首页不挂红条：

- CI 没过的提交不上线，CI 重跑通过了再上；两小时还没有 CI 结果也提示。
- 拉代码、重建失败，或者重启后 10 分钟 `/health` 还不通，提示失败的那一步。同一个提交最多试 3 轮，之后等下一个提交。
- Mac mini 上的仓库不在 main、有没提交的改动，或者 main 上有没推送的提交（都会被一起打进镜像），不上线，处理办法见下面。
- 取不到 main、查不到 CI（断网、GitHub 出错）超过 6 小时才提示。一装好就提示「取不到 origin/main」的，多半是 origin 用的 SSH、launchd 里读不到钥匙；仓库是公开的，改成 HTTPS：`git remote set-url origin https://github.com/Dongzl-2117/research.git`。

夜间补齐、定时抓取、备份，以及容器里手动起的 `research-translate`、`research-ocr` 正在跑时先不重启，等它们跑完：重建会把它们打断。这样等着本来不留记录：10-29 夜里合并的改动约 4 小时没上线，从外面看不出卡在哪。现在最早一个没上线的合并等了 3 小时还被挡着，或者上一轮自动上线跑了 2 小时还没完（比如重建卡住），`deploy.json` 记成失败，`research-status` 会打出来。以前按 main 上最新的提交算，10-31 一直有新合并，挡了四个多小时也没报。等不及就 `FORCE=1 scripts/deploy.sh`，正在跑的会被打断。

Mac mini 上的 `~/research` 只给自动上线用。在那台机器上改代码的人和 AI 另外 clone 一份（比如 `~/research-dev`）或者用 `git worktree`，改完推分支走 PR；不在 `~/research` 里切分支、改文件、提交。只在本机要忽略的文件写进 `.git/info/exclude`，不改 `.gitignore`。`data/status/deploy.json` 写着卡住了，按它说的那种处理：

- 不在 main：`git -C ~/research checkout main`。
- 有没提交的改动：先 `git -C ~/research diff` 看是什么，要留的推到分支走 PR，然后还原。只是本机加的忽略规则，比如 `.gitignore`，把新加的行挪进 `.git/info/exclude` 再还原：

  ```bash
  cd ~/research
  git diff -U0 .gitignore | grep '^+[^+]' | cut -c2- >> .git/info/exclude
  git checkout -- .gitignore
  ```

- main 上有没推送的提交：存到分支推上去开 PR，再让 main 回到 origin/main：

  ```bash
  cd ~/research
  git branch mini-local && git push origin mini-local
  git reset --keep origin/main
  ```

处理完不用做别的，下一轮（10 分钟内）自己上线。

马上上线（不等 CI 和别的任务，失败过 3 轮的也重试）：`FORCE=1 scripts/deploy.sh`。不要自动上线：`INSTALL_DEPLOY=0 scripts/install-launchd.sh`，之后照旧手动：

```bash
git pull
docker compose up -d --build
scripts/install-launchd.sh   # 定时任务有变化时再跑一次；上次的设置记在 data/launchd.env
```

图片文字（OCR）在 `app` 容器里调站内 deepseek-flash 识图，不用另外的镜像，见 [ocr.md](ocr.md)。

## 依赖锁死

镜像按 `requirements.lock` 装依赖（`pip install --require-hashes`，版本和 hash 都锁死），改代码重新部署不会顺手拉到新版本的 FastAPI、Starlette。`requirements-dev.lock` 给 CI 和本地测试用，版本和应用一致。

升级依赖：

```bash
scripts/lock.sh --upgrade-package fastapi   # 只升一个
scripts/lock.sh --upgrade                   # 全部升到最新
```

改了 `pyproject.toml` 的依赖以后跑 `scripts/lock.sh`（已锁的版本不动，只补新加的）。本机没有 uv 时脚本在 Python 容器里跑。只用发布满两周的版本：刚发布的版本偶尔有问题、被撤回，甚至被人投毒，等两周再用；急着要某个新版本时加 `COOLDOWN_DAYS=0`。改完的锁文件和代码一起提交；CI 会检查锁文件和 `pyproject.toml` 对得上、按 arm64（Mac mini）下得到全部的包，再用锁文件跑测试、构建镜像。

站内 LLM 默认 DeepSeek（`DEEPSEEK_API_KEY`，见 [llm.md](llm.md)）。Grok OAuth 是可选项：`RESEARCH_LLM_PROVIDER=grok` 时用本机 `data/secrets/xai-oauth.json`（容器内 `/data/secrets/xai-oauth.json`），登录用 `docker compose exec app research-xai-login login`。见 [translate.md](translate.md)。

改完来源配置（`config/sources.yaml`）或持仓名单（`config/portfolio.yaml`）后刷新页面即可。导入命令每次都会重新读配置。

## Cloudflare Tunnel

Access 和隧道都在应用外面做。应用里没有登录，也没有可以绕过的口令。

`cloudflared` 指向本机回环地址：

```yaml
ingress:
  - hostname: investment.zldong.com
    service: http://127.0.0.1:8088
  - service: http_status:404
```

在 Cloudflare Zero Trust 里给 `investment.zldong.com` 加 Access 策略，只放自己的身份。不要把源站端口暴露到公网。

## 磁盘

- 文章正文在 Postgres 卷 `pgdata`。
- 图片在 `./data/images`，由应用从 `/media/...` 读出。从外链下载来的在 `./data/images/remote/`。
- 定时任务的结果在 `./data/status/`。
- 待导入的压缩包放在 `./data/inbox`，不要提交进 git。

一篇归档的图片可能到几 GB 甚至更多。228GB 够当前几千篇文章，后面到几万篇时先看 `data/images` 和 Docker 卷的占用。

## 容器里为什么监听 0.0.0.0

进程在容器里必须监听 `0.0.0.0`，外面的端口发布才能进来。compose 同时设置 `ALLOW_NONLOCAL_BIND=1`。宿主机发布地址仍然是 `127.0.0.1`。不设这个开关时，进程拒绝绑定非回环地址。

## 改数据库密码

同时改 `.env` 里的 `POSTGRES_PASSWORD` 和 compose 使用的 `DATABASE_URL`。已经初始化过的数据卷不会自动改密码，需要进 `psql` 执行 `ALTER USER`，或在确认可以清空时删掉卷重建。

这个密码只给本机用，不要拿去当线上公网数据库的口令。

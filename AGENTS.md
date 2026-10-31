# 给维护这个仓库的 AI

这个站（investment.zldong.com）由几个 AI 一起维护，Zhilin 不逐条看对话。开工前读完这一页；规矩变了、挂着的事做完了，顺手改这一页。

## 先知道的三件事

1. **合并进 main 就会上线。** Mac mini 上的定时任务 `com.zldong.research.deploy` 每 10 分钟看一次 main，提交的 Tests 检查通过了就拉代码、`docker compose up -d --build`、等 `/health` 通（`scripts/deploy.sh`，见 [docs/deploy.md](docs/deploy.md)「更新」）。所以 main 上的每个提交都要能直接上线。要先在正式库上回填、核查才能上线的改动，留在分支或草稿 PR 里，PR 描述写清上线前提；合并别人的 PR 前，先看它自己写的上线前提满足没有。
2. **迁移在应用启动时跑。** `src/researchlib/migrations/` 下的 SQL 由 `db.prepare` 按文件名顺序执行，合并后在正式库上自动跑。迁移要对现有数据安全、能重复跑、几万行内很快跑完；删数据、全表改写要在 PR 里写清影响哪些行。编号接着 main 上最新的写，合并前看一眼有没有和别的分支撞号。
3. **自动上线认的是 CI 里名叫 Tests 的检查。** 改 `.github/workflows/ci.yml` 里这个 job 的名字，要同时改 `scripts/deploy.sh` 的 `DEPLOY_CHECK` 默认值。草稿 PR 不跑 CI，要看 CI 结果先转成待审。

## 正式环境

- Mac mini 上用 docker compose 跑，应用只听 `127.0.0.1:8088`，公网经 Cloudflare Tunnel。网站不登录就能看，核对线上改动可以直接读页面和 `/api/v1/articles/{id}.md`。
- 云端的 AI 连不上 Mac mini。只能在那边跑的命令（装定时任务、一次性的数据修复）写进 PR 描述的「上线后」一节，由 Zhilin 跑；平时合并就上线，不用他动手。
- Mac mini 上的 `~/research` 只给自动上线用。在那台机器上干活的人和 AI 另外 clone 一份或者用 `git worktree`，不在 `~/research` 里切分支、改文件、提交；本机才要忽略的文件写进 `.git/info/exclude`。那份仓库不在 main、有本机改动或者有没推送的提交，自动上线都会停下，原因写在 `data/status/deploy.json`，处理办法见 [docs/deploy.md](docs/deploy.md)「更新」。
- 定时任务（备份、试恢复、夜间补齐、方伟公众号和星球抓取、孤独进化体抓取、自动上线）的结果写进 `data/status/<任务>.json`，供值班助手看：`docker compose exec app research-status` 把失败和太久没成功的逐条打出来，有就退出码 1。首页不再挂运维红条，见 [docs/backup.md](docs/backup.md)。
- 密钥在 Mac mini 的 `.env`。站内 LLM 默认 DeepSeek，见 [docs/llm.md](docs/llm.md)。

## 怎么协作

- **信息都写进 PR 描述。** 为什么改、改了什么、上线后会发生什么（迁移、启动时的重算、要不要手动跑命令）、怎么核对生效、还剩什么没做。后来的 AI 从 PR 和这一页了解上下文，看不到你的对话。
- 开工前看一眼 open 的 PR 和最近合并的 PR，避免两个 AI 改同一处。以前 #56 和 #65 各自重写了同一批文件，冲突了 11 个文件。
- 大改动拆成能各自上线的小 PR，先合能独立上线的部分。
- 文档、PR、注释用中文。

## 规矩

- 仓库是公开的。不提交密钥、cookie、`.env`、`data/`、归档压缩包。
- 测试样本、注释、文档和提示词里的例子不放真人的名字：群友、评论者、截图上的网名都用虚构的（王小明、李四），只有来源作者本人（比如方伟）用真名。线上数据里的网名只在网站上显示，不进仓库。
- 宿主机脚本（`scripts/*.sh`）要在 macOS 自带的 bash 3.2 上能跑：变量后面紧跟中文时写成 `${变量}`；直接跑的脚本要有执行权限。结果用 `scripts/status.sh` 的 `run_step`、`status_ok`、`status_fail` 写状态文件。`tests/unit/test_scripts.py` 会查变量写法、语法和执行权限。
- 不在启动或上线时跑 `research-views`、`research-digest`、`research-theses` 这类花 LLM 钱的批量任务。夜间任务每晚有上限。要一次跑一大批，先在 PR 里写出篇数和大概费用，等 Zhilin 同意。这类任务（译制、摘要、观点、论点、识图）只有命令行入口 `docker compose exec app research-*`，网页和 API 触发它们的接口 #140 删了。
- 整个来源先不展示：把 `config/sources.yaml` 里那一项的 `enabled` 改成 `false`。从配置里删掉的来源，启动时 `sync_sources` 也记成停用。停用的来源网站、接口、问库都不出现，导入会拒绝，摘要、观点、论点不再处理，库里的行留着。
- 不跳过、不禁用测试来让 CI 变绿。

## 本地测试

```bash
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps -e .
ruff check .
pytest tests/unit
DATABASE_URL=postgresql://research:research@127.0.0.1:5432/research_test pytest tests/integration
```

集成测试要带 zhparser 和 pgvector 的 Postgres：`docker compose up -d db` 后建 `research_test` 库。连不上库时集成测试全部跳过，跳过不等于通过。以 root 跑时 `tests/unit/test_xai_oauth_owner.py::test_desired_owner_from_existing_file` 必定失败（它查文件属主），和改动无关。

没有 Docker（比如云端容器）时，本机装 Postgres 16 再编扩展：

1. `apt-get install postgresql-16 postgresql-server-dev-16 build-essential bzip2`
2. scws：解开 `docker/postgres/vendor/scws-1.2.3.tar.bz2`，`./configure --prefix=/usr/local && make install && ldconfig`
3. zhparser：用 `docker/postgres/Dockerfile` 里 `ZHPARSER_COMMIT` 那个提交的源码，`make && make install`
4. pgvector：Ubuntu 自带的包太旧（没有 halfvec），从源码编 0.8 以上
5. 建角色 `research`（密码 `research`，SUPERUSER）和库 `research_test`、`research_eval`，两个库里都 `CREATE EXTENSION zhparser; CREATE EXTENSION vector;`

改了检索相关的文件（清单在 `ci.yml` 的 `retrieval`）时再跑检索评测：

```bash
python tools/retrieval_eval/gen_corpus.py --articles 1500
python tools/retrieval_eval/evaluate.py --check tools/retrieval_eval/baseline.json
```

## 挂着的事（2026-10-31）

- **#56 合进 main 后撤回了**（撤回提交 58483db）。它是 Codex 做的星球账户资格和引用边界，分支 `codex/wechat-color-ci-savings-20261022`。重新合之前：按它分支上 `docs/availability-gate-2026-10-23.md` 的计划定向回填账户 ID、评论和图片文字；`TRUSTED_OWNERS` 里加上乘风破浪的星主账户，否则这个来源整个不显示；它把迁移从网页启动挪到了 `research-maintain`，`scripts/deploy.sh` 要先跑这一步再重启；和启动时按 `poster_names` 重算星主标记（`posters.refresh_poster_roles`）的做法冲突，要选一种。
- **#73（Codex 的草稿）重写了自动上线和启动**：从提交快照构建、Web 启动不跑迁移、健康检查失败退回旧镜像。它写明第一次上线要人工交接，不能让现在的 `scripts/deploy.sh` 直接把它拉上线；合它之前先读它的 PR 描述和 `docs/deploy-safety.md`。
- 孤独进化体以前从 AIHOT 来，10-30 起改由 `scripts/crawl-gudujinhuati.sh` 每天抓（`research-crawl-wechat`，极致了查列表 + 公众号原文页）。
- 停更的来源没有抓取：黑黑无常（一次性导入的雪球存档）、Stratechery（在另一台机器「盒子」上手动抓再导入）、乘风破浪（没有抓取脚本）。接不接、用什么账号由 Zhilin 定。
- 黑黑无常的短帖没有标题，导入时拿正文第一段当了标题，库里 39 篇有 24 篇长过 40 字。页面和接口显示时收成第一句（#180），检索和问库喂给模型的还是整段。
- 段永平博客 10-30 下线（#144）：配置项、导入命令和只为它写的标记都删了，库里的文章、提及、观点、摘要留着。要不要删库里的行由 Zhilin 定，删的话另开 PR 写清表和行数。
- 10-28 开的一批 PR 落后 main，接着做先基于 main 重做：#76、#78、#81、#101、#102 已和 main 冲突。#140 删了 `jobs/` 和批量任务的 HTTP 接口，#76（只许本机调用这些接口）因此作废；#144 删了 `empty_answer.py`，`visible_sql`、`HIDDEN`、`SHORT_POST` 挪到 `researchlib.visibility`，隐藏标记只认 `hidden`，`suppressed`、`source_paused` 不再认。
- 网站不展示知识星球读者帖（首页/按天/检索/正文/API 均隐藏，库内保留）；标题为「图片」评论（N）… 的评论壳帖同样隐藏（poster_role 多为 owner）。两种都不抽观点、不写摘要、不识图，已有的观点不进公司页。方伟星球 10 月 20 日以后被误标成读者帖的那批，修好 poster_role 后才会重新露出来；观点靠夜间每晚补 60 篇，一次补完要花 DeepSeek 的钱，没做。
- 林文丰飞书里的内嵌电子表格只有占位文字，要重新导出才能补。
- 21118、22195 的长英文重复在对照页按位置收：近重复且中文是同一段（更短或措辞不同也算）收掉后一对；短句整句包含在更长的一段里，或较短那句大约四分之三是较长那句的开头且长度接近，收短句、留长段。正好 80 字的一句不因标签贴着句号少算一格。不必为这些重复再译。若对照仍是两千字的一整对，才点名重译，命令见 [docs/translate.md](docs/translate.md)。21130 的叶子数已经对齐，同一条规则收近重复。
- **Mac mini 上还没跑的一次性命令**（都不调 LLM，先加 `--dry-run` 看篇数；跑完删掉那一行）：
  - `docker compose exec app research-retime --source chengfengpolang`：乘风破浪的发布时间从当天零点改成归档头里的钟点。不跑，乘风破浪的卡片时间一直是「—」，同一天的先后按入库顺序。
  - `docker compose exec app research-clean --source chengfengpolang --source fangwei-zsxq --source linwenfeng-feishu --source stratechery`：星球长文帖已入库的检索文字和段落块去掉导出脚手架（#124），星球老帖检索文字开头那行导出时截的「前十几个字...」也一并去掉；林文丰飞书和 Stratechery 的检索文字去掉图片文件名（「image.png」「截屏2025-06-08 19.36.10.png」「Screen Shot 2017-06-01 at 7.40.43 PM」）和包在图外的大图地址（#192），公式里的乘号和词中间的下划线照原样留着（#194，以前「eCPM=CPA_bid*CTR*CVR*1000」成了「eCPM=CPAbidCTRCVR1000」）；Stratechery 每日更新开头的「Listen to this post:」「Good morning,」「On to the Update:」和结尾的播客提示、转发须知、「have a great day」不再进检索文字和段落块（#199；标题行下面不空行的邮件导出和一行一段的 2026 网页导出也认，#206）。不跑，检索片段和问库里长文开头多一遍，飞书约 300 篇、Stratechery 约 290 篇的检索片段夹着文件名和 wp-content 地址，搜 subscriptions 这类词 Stratechery 命中 1700 多篇、片段常是转发须知；页面上的标题和卡片摘录已经绕开导出标题那一行。
  - `docker compose exec app research-repair-zsxq --source chengfengpolang`：乘风破浪的标题写回库（#128），评论区只留星主（#151）。不跑，页面和 API 现算、现滤，可检索和问库还按库里的旧标题，也还搜得到读者的评论和昵称。它要等的 #155 已上线（10-31 核过：线上 879 的评论区是「郑立涛 回复网友」），现在可以跑。
  - `docker compose exec app research-rebuild-chunks --ocr`：检索里的图片文字块按现在的整理规则重切（#147）。不跑，检索和问库里 451 篇方伟公众号还搜得到末尾的星球优惠券卡，69 篇群聊截图带着「你邀请"某某"加入了群聊」和群友昵称；分几张截的长群聊，重叠的整句在相邻两块里各一遍（重切后只留前一块的）。只读已识别的字，不识图；改过的块由夜间任务里的 `research-embed`（本机 Ollama）重算向量。

- 通用原文相关性标注采用独立本机服务和 SQLite，见 [docs/relevance-annotations.md](docs/relevance-annotations.md)。主站不挂该入口，不跑迁移或模型；私有原文、标签及导出只留 `data/relevance/` 等私有目录。实际库 provider、远程认证与部署需另行配置，demo 不是真实库。

# 给维护这个仓库的 AI

这个站（investment.zldong.com）由几个 AI 一起维护，Zhilin 不逐条看对话。开工前读完这一页；规矩变了、挂着的事做完了，顺手改这一页。

## 先知道的三件事

1. **合并进 main 就会上线。** Mac mini 上的定时任务 `com.zldong.research.deploy` 每 10 分钟看一次 main，提交的 Tests 检查通过了就拉代码、`docker compose up -d --build`、等 `/health` 通（`scripts/deploy.sh`，见 [docs/deploy.md](docs/deploy.md)「更新」）。所以 main 上的每个提交都要能直接上线。要先在正式库上回填、核查才能上线的改动，留在分支或草稿 PR 里，PR 描述写清上线前提；合并别人的 PR 前，先看它自己写的上线前提满足没有。
2. **迁移在应用启动时跑。** `src/researchlib/migrations/` 下的 SQL 由 `db.prepare` 按文件名顺序执行，合并后在正式库上自动跑。迁移要对现有数据安全、能重复跑、几万行内很快跑完；删数据、全表改写要在 PR 里写清影响哪些行。编号接着 main 上最新的写，合并前看一眼有没有和别的分支撞号。
3. **自动上线认的是 CI 里名叫 Tests 的检查。** 改 `.github/workflows/ci.yml` 里这个 job 的名字，要同时改 `scripts/deploy.sh` 的 `DEPLOY_CHECK` 默认值。草稿 PR 不跑 CI，要看 CI 结果先转成待审。

## 正式环境

- Mac mini 上用 docker compose 跑，应用只听 `127.0.0.1:8088`，公网经 Cloudflare Tunnel。网站不登录就能看，核对线上改动可以直接读页面和 `/api/v1/articles/{id}.md`。
- 云端的 AI 连不上 Mac mini。只能在那边跑的命令（装定时任务、一次性的数据修复）写进 PR 描述的「上线后」一节，由 Zhilin 跑；平时合并就上线，不用他动手。
- Mac mini 上的 `~/research` 只给自动上线用。在那台机器上干活的人和 AI 另外 clone 一份或者用 `git worktree`，不在 `~/research` 里切分支、改文件、提交；本机才要忽略的文件写进 `.git/info/exclude`。那份仓库不在 main、有本机改动或者有没推送的提交，自动上线都会停下，首页提示一行，处理办法见 [docs/deploy.md](docs/deploy.md)「更新」。
- 定时任务（备份、试恢复、夜间补齐、方伟公众号和星球抓取、自动上线）的结果写进 `data/status/<任务>.json`，供值班助手看；首页不再挂运维红条，见 [docs/backup.md](docs/backup.md)。
- 密钥在 Mac mini 的 `.env`。站内 LLM 默认 DeepSeek，见 [docs/llm.md](docs/llm.md)。

## 怎么协作

- **信息都写进 PR 描述。** 为什么改、改了什么、上线后会发生什么（迁移、启动时的重算、要不要手动跑命令）、怎么核对生效、还剩什么没做。后来的 AI 从 PR 和这一页了解上下文，看不到你的对话。
- 开工前看一眼 open 的 PR 和最近合并的 PR，避免两个 AI 改同一处。以前 #56 和 #65 各自重写了同一批文件，冲突了 11 个文件。
- 大改动拆成能各自上线的小 PR，先合能独立上线的部分。
- 文档、PR、注释用中文。

## 规矩

- 仓库是公开的。不提交密钥、cookie、`.env`、`data/`、归档压缩包。
- 测试样本不放真人的名字：群友、评论者用虚构的（王小明、李四），只有来源作者本人（比如方伟）用真名。
- 宿主机脚本（`scripts/*.sh`）要在 macOS 自带的 bash 3.2 上能跑：变量后面紧跟中文时写成 `${变量}`；直接跑的脚本要有执行权限。结果用 `scripts/status.sh` 的 `run_step`、`status_ok`、`status_fail` 写状态文件。`tests/unit/test_scripts.py` 会查变量写法、语法和执行权限。
- 不在启动或上线时跑 `research-views`、`research-digest`、`research-theses` 这类花 LLM 钱的批量任务。夜间任务每晚有上限。要一次跑一大批，先在 PR 里写出篇数和大概费用，等 Zhilin 同意。
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

## 挂着的事（2026-10-27）

- 搜索片段专题见 [docs/search-passages.md](docs/search-passages.md)：主体与主题分开排序、旧译文/向量版本过滤、独立片段回链。真实双语模型效果尚未验收；词法与固定向量契约测试不能充当语义质量证据。原检索基线不得降低。

- **#56 合进 main 后撤回了**（撤回提交 58483db）。它是 Codex 做的星球账户资格和引用边界，分支 `codex/wechat-color-ci-savings-20261022`。重新合之前：按它分支上 `docs/availability-gate-2026-10-23.md` 的计划定向回填账户 ID、评论和图片文字；`TRUSTED_OWNERS` 里加上乘风破浪的星主账户，否则这个来源整个不显示；它把迁移从网页启动挪到了 `research-maintain`，`scripts/deploy.sh` 要先跑这一步再重启；和启动时按 `poster_names` 重算星主标记（`posters.refresh_poster_roles`）的做法冲突，要选一种。
- **自动上线卡在 Mac mini 的本机改动上**：10-27 已经装好并上线过（线上的 demo 文章 8879 已删，#74 的 `/api/v1/digest` 能打开），10-27 22:00（北京时间）首页提示 `~/research` 的 `.gitignore` 有本机改动，之后合并的 #80 没上线。处理命令已经交给 Zhilin（docs/deploy.md「更新」）。首页的「自动上线失败」提示消失就是好了，这条随后删掉。
- **#73（Codex 的草稿）重写了自动上线和启动**：从提交快照构建、Web 启动不跑迁移、健康检查失败退回旧镜像。它写明第一次上线要人工交接，不能让现在的 `scripts/deploy.sh` 直接把它拉上线；合它之前先读它的 PR 描述和 `docs/deploy-safety.md`。
- 停更的来源没有抓取：孤独进化体（来自已下线的 AIHOT）、黑黑无常（一次性导入的雪球存档）、Stratechery（在另一台机器「盒子」上手动抓再导入）、乘风破浪（没有抓取脚本）。接不接、用什么账号由 Zhilin 定。
- 网站不展示知识星球读者帖（首页/按天/检索/正文/API 均隐藏，库内保留）；标题为「图片」评论（N）… 的评论壳帖同样隐藏（poster_role 多为 owner）。方伟星球 10 月 20 日以后被误标成读者帖的那批，修好 poster_role 后才会重新露出来；观点靠夜间每晚补 60 篇，一次补完要花 DeepSeek 的钱，没做。
- 林文丰飞书里的内嵌电子表格只有占位文字，要重新导出才能补。

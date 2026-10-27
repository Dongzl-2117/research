# 给维护这个仓库的 AI

这个站（investment.zldong.com）由几个 AI 一起维护，Zhilin 不逐条看对话。开工前读完这一页；规矩变了、挂着的事做完了，顺手改这一页。

## 先知道的三件事

1. **本次部署保护需先人工交接。** 旧定时任务是否已安装仍未知，不能据 PR 描述推定线上状态。合并前先读 [docs/deploy-safety.md](docs/deploy-safety.md)，暂停旧调度、等任务完成、核对备份与迁移；本次草稿不能让旧 deploy.sh 自动上线。交接完成后的自动部署仅构建 Tests 已通过的固定 SHA，保留宿主机工作区；宿主机脚本/launchd 变更要显式安装。
2. **Web 默认只读启动。** 只核对数据库迁移集合，不迁移、不重算角色/块/提及、不清理观点。有未执行迁移或数据库比代码新时拒绝启动。迁移和维护要在校验/试恢复备份、明确数据影响之后显式执行，不能认为换回镜像就恢复数据。本 PR 不新增 SQL 迁移。
3. **Tests 仍是部署门槛。** 草稿 PR 的 Tests 会跳过；可以按既有流程临时转待审跑完整 CI，完成后转回草稿。分别报告实际执行的测试与最终检查状态，不能把 skipped 当通过。禁止改低基线或绕过检查。

## 正式环境

- Mac mini 上用 docker compose 跑，应用只听 `127.0.0.1:8088`，公网经 Cloudflare Tunnel。网站不登录就能看，核对线上改动可以直接读页面和 `/api/v1/articles/{id}.md`。
- AI 连不上 Mac mini。只能在那边跑的命令（装定时任务、一次性的数据修复）写进 PR 描述的「上线后」一节，由 Zhilin 跑；平时合并就上线，不用他动手。
- 定时任务（备份、试恢复、夜间补齐、方伟公众号和星球抓取、自动上线）的结果写进 `data/status/<任务>.json`，首页顶上只在出问题时提示一行，见 [docs/backup.md](docs/backup.md)。
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

## 挂着的事（2026-10-26）

- **#56 合进 main 后撤回了**（撤回提交 58483db）。它是 Codex 做的星球账户资格和引用边界，分支 `codex/wechat-color-ci-savings-20261022`。重新合之前：按它分支上 `docs/availability-gate-2026-10-23.md` 的计划定向回填账户 ID、评论和图片文字；`TRUSTED_OWNERS` 里加上乘风破浪的星主账户，否则这个来源整个不显示；它把迁移从网页启动挪到了 `research-maintain`，`scripts/deploy.sh` 要先跑这一步再重启；和启动时按 `poster_names` 重算星主标记（`posters.refresh_poster_roles`）的做法冲突，要选一种。
- **自动上线要在 Mac mini 上装一次**：`cd ~/research && git pull && scripts/install-launchd.sh`。2026-10-26 已经交给 Zhilin，装没装可以看线上的文章 8879（乘风破浪的 demo 文章，迁移 023 会删掉）还在不在。
- 停更的来源没有抓取：孤独进化体（来自已下线的 AIHOT）、黑黑无常（一次性导入的雪球存档）、Stratechery（在另一台机器「盒子」上手动抓再导入）、乘风破浪（没有抓取脚本）。接不接、用什么账号由 Zhilin 定。
- 方伟星球 10 月 20 日以后被误当成读者帖的那批，观点靠夜间每晚补 60 篇；一次补完要花 DeepSeek 的钱，没做。
- 林文丰飞书里的内嵌电子表格只有占位文字，要重新导出才能补。

## 本次安全修复的交接

见 [docs/deploy-safety.md](docs/deploy-safety.md)。本次只选择性恢复账户身份、向量一致性和文本边界；不恢复 #56 的整源门禁。乘风破浪未登记、方伟旧帖缺账户都保留原文可见，身份明确标 unknown。账户登记来源于 #56 的公开证据，合并前在 mini 复核；昵称不能认证账户。公司页 GET 不排队付费生成，继续用限额 CLI/夜间任务。角色重算保留旧观点，不自动付费重写。测试只用隔离库及合成样本。

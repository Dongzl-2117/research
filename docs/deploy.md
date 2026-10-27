# 部署

站点域名是 **investment.zldong.com**。仓库名仍然是 `Dongzl-2117/research`。

目标机器是 Mac mini（M4，16GB 内存，大约 228GB 磁盘）。服务用 docker compose 跑，Postgres 把 `shared_buffers` 限制在 256MB，避免和本机其他进程抢内存。

## 第一次启动

```bash
cp .env.example .env
docker compose up -d --build
```

应用监听容器内的 `8088`，compose 只把宿主机的 **127.0.0.1:8088** 转进去。不要改成 `0.0.0.0`。数据库端口同样只绑定 `127.0.0.1:5432`。

Web 现在默认只读启动：核对迁移集合，不执行迁移、角色重算、补块、问库状态清理或后台全库重算。首次初始化及已有库维护必须显式执行；备份、迁移检查、首次人工交接、运行方式和回退边界见 [deploy-safety.md](deploy-safety.md)。未完成数据库维护时启动会失败，不会偷偷补做迁移。

原有 CLI 仍可用于有计划的维护；`db.prepare` 是有数据写入的维护接口，必须先核对备份和数据影响。去重、公司提及重算、日期/目录修复按需要显式跑。公司页 GET 只展示已有摘要、观点、论点；付费生成由有额度的 CLI/夜间任务执行。其他页面既有的卡片摘要排队未整体改造。

备份、试恢复、夜间补齐和定时抓取仍使用 launchd，见 [backup.md](backup.md)。本次自动部署变更需要先按安全交接文档安装新脚本，不能直接让旧自动部署接手。

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

更新必须先完成 [安全交接](deploy-safety.md)。交接后定时任务每 10 分钟检查 main；Tests 的最新匹配检查通过后，从固定 SHA 的独立快照构建并仅切换 app，健康检查同时核对返回 SHA。迁移不在这条自动路径执行；旧运行版本没有协调协议、任务仍忙、迁移集合不符时默认拒绝切换。

`FORCE=1 scripts/deploy.sh` 只重试失败提交，不跳过 CI 或任务保护。宿主机使用共享/独占文件锁，独立容器任务使用数据库会话锁；不强制终止长任务。健康失败时只在兼容的只读启动边界内尝试恢复旧镜像和配置，不恢复数据。运行配置保存在 `data/releases/<SHA>/`，维护者的 Git 工作区不自动更新；宿主机脚本与定时任务模板的变化必须显式安装。

mini 的自动任务是否启用和线上 SHA 要现场核实。日志/状态文件仍在原位置。不要直接用本机脏目录 `docker compose up --build` 代替固定 SHA 发布，具体操作、首次部署与回退前提见交接文档。

图片文字（OCR）用单独的镜像，要用时另外构建，见 [ocr.md](ocr.md)。

## 依赖锁死

镜像按 `requirements.lock` 装依赖（`pip install --require-hashes`，版本和 hash 都锁死），改代码重新部署不会顺手拉到新版本的 FastAPI、Starlette。`requirements-dev.lock` 给 CI 和本地测试用，`requirements-ocr.lock` 给 OCR 镜像用，版本和应用一致。

升级依赖：

```bash
scripts/lock.sh --upgrade-package fastapi   # 只升一个
scripts/lock.sh --upgrade                   # 全部升到最新
```

改了 `pyproject.toml` 的依赖以后跑 `scripts/lock.sh`（已锁的版本不动，只补新加的）。本机没有 uv 时脚本在 Python 容器里跑。只用发布满两周的版本：刚发布的版本偶尔有问题、被撤回，甚至被人投毒，等两周再用；急着要某个新版本时加 `COOLDOWN_DAYS=0`。改完的锁文件和代码一起提交；CI 会检查锁文件和 `pyproject.toml` 对得上、按 arm64（Mac mini）下得到全部的包，再用锁文件跑测试、构建镜像。

站内 LLM 默认 DeepSeek（`DEEPSEEK_API_KEY`，见 [llm.md](llm.md)）。Grok OAuth 是可选项：`RESEARCH_LLM_PROVIDER=grok` 时用本机 `data/secrets/xai-oauth.json`（容器内 `/data/secrets/xai-oauth.json`）。AIHOT 已下线，不再挂外部卷 `aihot_data`。见 [translate.md](translate.md)。

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

# 部署

站点域名是 **investment.zldong.com**。仓库名仍然是 `Dongzl-2117/research`。

目标机器是 Mac mini（M4，16GB 内存，大约 228GB 磁盘）。服务用 docker compose 跑，Postgres 把 `shared_buffers` 限制在 256MB，避免和本机其他进程抢内存。

开发接手先看 [README](../README.md#开发)。本页后面的服务初始化和更新是部署流程，不应在运行中的 checkout 直接照做；暂停工作进程、切换服务、写生产数据和恢复均需独立安排。

## 一次性集成测试环境

仅在无 `.env`、无生产 `data/` 的独立 clone 中、安装好 README 所述开发依赖后使用。Docker daemon 可以来自本机 Docker Desktop、Colima 或其他正常安装；不要求云环境。以下 Bash 示例只构建仓库已有 `docker/postgres/Dockerfile`，创建唯一项目、空测试卷及随机本机回环端口，不加载生产 compose、配置、卷或模型。

```bash
bash <<'BASH'
set -euo pipefail
test ! -e .env || { echo '请使用不含 .env 的独立 clone' >&2; exit 1; }
test -x .venv/bin/python
test_root=$(mktemp -d "${TMPDIR:-/tmp}/research-pg.XXXXXX")
test_project="research-test-$(date +%s)-$$"
test_image="research-test-db:$test_project"
printf '测试目录：%s\n项目：%s\n' "$test_root" "$test_project"
docker build -t "$test_image" docker/postgres
cat > "$test_root/compose.yaml" <<YAML
services:
  db:
    image: "$test_image"
    environment:
      POSTGRES_USER: research
      POSTGRES_PASSWORD: test-only
      POSTGRES_DB: research_test
      LANG: C.UTF-8
    ports: ["127.0.0.1::5432"]
    volumes: ["test_pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U research -d research_test"]
      interval: 2s
      timeout: 2s
      retries: 30
volumes:
  test_pgdata:
YAML
test_compose=(docker compose --env-file /dev/null --project-directory "$test_root" -p "$test_project" -f "$test_root/compose.yaml")
"${test_compose[@]}" up -d --wait --wait-timeout 90
test_addr=$("${test_compose[@]}" port db 5432)
case "$test_addr" in 127.0.0.1:*) ;; *) echo '非预期测试端口' >&2; exit 1;; esac
test_port=${test_addr##*:}
case "$test_port" in ''|*[!0-9]*) echo '无效测试端口' >&2; exit 1;; esac
"${test_compose[@]}" exec -T db psql -U research -d research_test -c \
  "SELECT name,default_version,installed_version FROM pg_available_extensions WHERE name IN ('zhparser','vector');"
mkdir -p "$test_root/tmp" "$test_root/data"
env -i PATH="$PWD/.venv/bin:$PATH" TMPDIR="$test_root/tmp" \
  DATA_DIR="$test_root/data" XAI_OAUTH_TOKEN_PATH="$test_root/no-oauth.json" \
  DATABASE_URL="postgresql://research:test-only@127.0.0.1:$test_port/research_test" \
  PGOPTIONS='-c statement_timeout=5000 -c lock_timeout=1000' \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest tests/integration -q
# 检查结果后只删除本脚本创建的项目/测试卷；失败时也用这一完整项目与文件参数清理。
"${test_compose[@]}" down -v
printf '测试记录目录：%s\n清理项目：%s\n' "$test_root" "$test_project"
BASH
```

失败时脚本会停止，保留临时路径和项目以便检查；不要改用无 `-p/-f` 的生产 compose 清理。`docker build` 需访问 Dockerfile 指定的官方基础镜像、PGDG 包及固定 zhparser 源码，未在这里升级依赖或替换中文解析器。Apple Silicon 使用 arm64、常规 Linux 使用 amd64；记录实际镜像 ID、Python、PG 与扩展版本。Dockerfile 固定 PG 主版本和 zhparser 源码，但基础镜像标签/PGDG 包可更新，不承诺每次扩展补丁版本相同。

这两个文件使用合成资料和假向量/生成函数，不需 API key，也不重算真实文章向量。确认 pytest 的 passed/failed/skipped 数量；扩展缺失或连库失败时可能全部 skip，退出码 0 不等于已验证。完整 `tests/integration` 和合成检索评测仍是另一次测试范围，不能把固定 12 项当全套；测试 fixture 的迁移与清空操作只允许发生在此空卷。

## 第一次启动

```bash
cp .env.example .env
docker compose build app
docker compose up -d db
docker compose run --rm app research-maintain
docker compose up -d app
```

应用监听容器内的 `8088`，compose 只把宿主机的 **127.0.0.1:8088** 转进去。不要改成 `0.0.0.0`。数据库端口同样只绑定 `127.0.0.1:5432`。

Web 启动和所有 GET/HEAD 页面只读取已保存的结果，不执行迁移、同步来源、补切段落、整理 OCR 或安排模型任务。读取数据库的事务在第一条查询前设为只读，结束时回滚。

数据库初始化和升级由维护者显式运行 `research-maintain`：按顺序执行待应用的迁移、同步 `config/sources.yaml`、写入公司种子、补齐缺少的段落块，并把上次中断的问库任务标记为错误。该命令会写库；运行前应按 [backup.md](backup.md) 保留备份，不把它放进 Web 启动流程。

`019` 只建飞书目录表。部署后若林文丰已经在库里，跑一次 `research-doc-tree --source linwenfeng-feishu`（不访问飞书）。方伟星球若要改成真实 `create_time`，跑 `research-retime`（见 [ingest.md](ingest.md)），它会按新的 `published_at` 重排观点时间线和「同期别人怎么看」。这两条都不要在部署时自动跑。

`015_title_search.sql` 给文章表和译文表各加一列标题的全文索引，要重写一遍表：几千篇几秒钟，期间应用还没起来。`014_llm_inputs.sql` 把以前只读了长文开头写出来的摘要和观点标出来，之后按新的读法慢慢重写（见 [llm.md](llm.md)），不在部署时跑。

重复文章指纹和公司提及由显式维护命令检查，不再由 Web 启动线程或页面访问触发：

```bash
docker compose run --rm app research-maintain --upkeep
```

`--upkeep` 在初始化后依次检查重复文章、在公司别名或歧义规则变化时重算公司提及。首次执行可能涉及全库，安排在维护窗口；普通阅读无需运行它。也可按需单独使用 `research-dedupe` 和 `research-rebuild-mentions`。

备份、试恢复、夜间补齐和定时增量导入用 launchd 跑，见 [backup.md](backup.md)。这几个任务出了问题，首页顶上提示一行。

不会在启动或部署时跑 `research-digest`、`research-views`、`research-theses`。打开页面不会排队。需要生成或刷新时，使用明确的处理命令或已有夜间任务，见 [llm.md](llm.md)。

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

服务卸载闲置模型后，首次查询可能需要重新加载。本组合保留主线的有界冷启动等待：冷态最多 15 秒，普通或刚失败后的查询最多 4 秒，详见检索文档。是否常驻模型涉及额外内存和服务配置，由维护者另行评估；本轮没有调整 keep-alive、重启模型服务或做真实模型性能验收，这不是部署的默认步骤。

## 更新

本轮 PR56 整合 `main c57705e`（含 PR65/60/66/67），保留严格账户/来源/缓存边界和 HTML 清理、采集调度等有效修复。PR56 尚未合并，生产未按该候选切换；此处只读启动等行为针对 PR56，不能对着现库试启动。
PR56 保留严格身份与缓存门槛，不能当作保持旧展示资格的兼容只读保护方案的等价替换。
已有无维护翻译恢复入口绑定的是兼容候选的文件哈希；不能直接用于本整合候选，需另行绑定并验证，旧入口和旧镜像保留不变。
本次没有运行模型、生产回填或六篇定向写入；旧缓存和 OCR 证明缺口没有自动补齐。不得把单测通过当作这些部署条件已满足。

PR 56 的身份与缓存规则存在历史内容可用性前置条件，见 [有界核查与定向修复计划](availability-gate-2026-10-23.md)。
先核对历史星主回复和 OCR 的恢复范围，再决定上线；构建镜像或运行维护命令不会自动补足这些证明。

既有生产库不能把「拉取代码 → research-maintain → 重启 → 安装定时任务」作为默认升级流程。先确定目标提交与实际运行入口、可恢复备份、兼容回滚制品和历史可用性，再分别确认代码切换与所需数据操作。`021_zsxq_topic_ids.sql` 含全表元数据回填；本轮只在一次性空测试库执行迁移，不代表获准在现库运行。六篇定向修复不包含该全表操作。不要在接手或测试时安装任务、暂停/恢复翻译、回填、重建或执行维护。

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

改完来源配置（`config/sources.yaml`）或持仓名单（`config/portfolio.yaml`）后，刷新页面会更新内存中的筛选和导航；不会写数据库。来源表需要持久同步时显式运行 `research-maintain`，导入命令每次也会重新读配置。

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

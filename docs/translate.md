# 站内译制（中英对照）

英文原文入库后，由 **investment / research 站内** 调 LLM 翻译（默认 DeepSeek，可切 Grok OAuth），排版对齐 AIHOT 文章页：

- 默认 **对照**：每段英文在上（小一号、次要色 `--ink-4`），中文在下
- Tab：**对照 / 中文 / 原文**（`/articles/{id}`、`/zh`、`/original`）
- 媒体与图片只在英文侧出现一次
- 默认 `deepseek-v4-pro`：thinking 打开、`reasoning_effort=max`、上下文 1M token、输出至少 384000，不发 temperature。`RESEARCH_LLM_PROVIDER=grok` 时改用 `grok-4.7`、`reasoning_effort=xhigh`、上下文 50 万、输出至少 128000，不用 fast。常量在 `researchlib/deepseek.py` / `researchlib/grok.py`，问库、摘要、观点、论点、样卡走同一套

不要站外译完再贴进库。译文表是 `article_translations`，按 `content_hash` 与原文对齐。

标题另译一次（存 `title_zh`），失败不影响正文。译文写入后切成中文段落块进检索（`article_chunks` 里 `lang = 'zh'`，带对照页锚点），用中文能搜到、问到英文文章，见 [retrieval.md](retrieval.md)。启动时会给有译文但还没有译文块的文章补切。同一篇的重复不译。

## 和 AIHOT 的关系（仅 `RESEARCH_LLM_PROVIDER=grok`）

| | AIHOT | research |
|---|---|---|
| OAuth | research `data/secrets/xai-oauth.json` | **同一文件**（挂载为 `/data/secrets/xai-oauth.json`） |
| 登录 | `node scripts/xai-login.ts login`（主入口） | `research-xai-login status` 检查即可；一般**不必**再 login |
| 调用 | worker `translatePending` / 读者点译 | 站内 `POST /api/v1/translate`（正式）；`research-translate` 只用于开发和排障 |
| 展示 | `.bilingual-en` / `.bilingual-zh` | 同名 class，同 token |

Mac mini 上 research 的 `docker-compose.yml` 把 `./data` 挂到 `/data`，OAuth 路径：

- `XAI_OAUTH_TOKEN_PATH=/data/secrets/xai-oauth.json`
- `XAI_OAUTH_FILE_UID=1000` / `XAI_OAUTH_FILE_GID=1000`（AIHOT 已下线；research 进程可写回该文件）

双方共用**同一路径**和同一 `.lock`，轮流刷新；不要再复制一份 token。Hermes 的 `auth.json` 格式会被拒绝。

`research-xai-login logout` 会删掉共用文件，AIHOT 也会退出，慎用。

问库、`research-digest` 和页面上缺摘要的卡片读的是同一份凭证：默认 DeepSeek 的 `DEEPSEEK_API_KEY`，Grok 路径是同一份 token。应用启动和部署不会自动跑全库。

## 登录（Mac mini）

AIHOT 已登录时，research **无需再授权**：

```bash
cd ~/research
docker compose exec app research-xai-login status
# 应显示已登录，token 路径在 /data/secrets/xai-oauth.json
```

若两边都未登录，在 AIHOT 仓库完成一次即可：

```bash
cd ~/aihot
docker compose exec api node scripts/xai-login.ts login
```

## 译制：站内 API（正式触发方式）

生产续跑走站内接口。应用进程里起一个后台线程，复用 `translate.service` 的
`list_pending_english` / `translate_article`，和 DeepSeek / Grok 那套 `translate.llm` 共用凭证。
应用启动和部署都**不会**自动开译制，必须显式触发；同一时间只有一个 worker。

```bash
# 触发 stratechery 续跑：每轮取 10 篇，队列空了 60 秒后再扫
# 本机 curl 没有 Origin 头，和 /api/v1/ask 一样放行
curl -s http://127.0.0.1:8088/api/v1/translate \
  -H 'content-type: application/json' \
  -d '{"source":"stratechery","limit":10,"loop":true,"sleep":60}'

# 看进度
curl -s http://127.0.0.1:8088/api/v1/translate

# 停：正在译的那篇会译完，loop 里的休眠立刻醒
curl -s -X POST http://127.0.0.1:8088/api/v1/translate/stop
```

POST 字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `source` | 空 | 只译该来源 id；空表示所有来源 |
| `limit` | 5 | 每轮从待译队列取几篇，1–100 |
| `loop` | false | 队列空后休眠再扫，直到 stop |
| `sleep` | 60 | loop 空闲休眠秒数，5–3600 |

POST 立刻返回，实际翻译在后台线程里跑。返回 `{running, started, source, limit, loop, sleep, provider, model, started_at, ...}`；
`started=false` 表示已有 worker 在跑，这次没开第二个。
GET 同一路径返回 `{running, source, limit, loop, sleep, provider, model, started_at, finished_at, completed_this_run, failed_this_run, skipped_this_run, last_article_id, last_status, last_reason, last_error, waiting, stop_requested, thread_alive}`，不含任何 key / token。

同一轮里失败过（或没有可译正文）的 id 先跳过，队列空了睡一觉后再给一次机会，和 CLI 的逻辑一致。
worker 跟着应用进程走：容器重启或部署后就停了，要续跑再 POST 一次。
没配 LLM 凭证时 POST 返回 409，不会起线程空转。

CLI 保留给开发和排障，生产续跑不用它：

```bash
docker compose exec app research-translate --article-id 123
docker compose exec app research-translate --limit 10
docker compose exec app research-translate --source stratechery --limit 5
docker compose exec app research-translate --loop --sleep 120 --limit 3
```

调用参数与 AIHOT 一致：`https://api.x.ai/v1/chat/completions`，Bearer 为 OAuth access token；过期或 403 stale 时强制刷新再试。长调用默认 25 分钟（`LLM_LONG_TIMEOUT_MS`）。

## Stratechery

`config/sources.yaml` 里 `stratechery` 现为 **enabled: false**。抓取在盒子跑完、且只有 Passport 全文 / Daily≥5500 合格稿后：

1. 把 `enabled` 改为 `true`
2. `research-ingest-markdown --source stratechery --input …`
3. `curl -s http://127.0.0.1:8088/api/v1/translate -H 'content-type: application/json' -d '{"source":"stratechery","limit":10,"loop":true,"sleep":60}'`

游客半截 / guest teaser **不入库、不译**。

## 何时开跑翻译

| 状态 | 动作 |
|---|---|
| 译制管道已合入、LLM 凭证已配（DeepSeek key，或 AIHOT Grok 已 login 且 `RESEARCH_LLM_PROVIDER=grok`） | 可对库内已有合格英文稿 `POST /api/v1/translate` |
| Stratechery 仍在抓 | **先不 ingest**；可用英文样例验对照页 |
| 抓完并 ingest 后 | 对该来源开 `loop` 批量译 |

## 环境变量

见 `.env.example`。默认 DeepSeek：`RESEARCH_LLM_PROVIDER=deepseek`、`DEEPSEEK_API_KEY`，可选 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_REASONING_EFFORT`、`DEEPSEEK_MAX_TOKENS`。Grok 路径：`XAI_OAUTH_TOKEN_PATH`、`XAI_OAUTH_FILE_UID`、`XAI_OAUTH_FILE_GID`、`XAI_BASE_URL`、`XAI_MAX_TOKENS`。两边共用 `LLM_LONG_TIMEOUT_MS`。

## content_hash 与待译队列

`article_translations.content_hash` 必须等于 `articles.content_hash` 且 `complete`，这篇才算译完。`content_hash()` 把 `published_at` 固定按 Asia/Shanghai 打戳；若按 UTC `isoformat()` 重算会对不上，待译队列会把已译篇反复拿出来（`--limit 2` 时整队像卡住）。

时区漂移或改过日期后，跑：

```bash
PYTHONPATH=src python scripts/repair_content_hash_tz.py          # dry-run
PYTHONPATH=src python scripts/repair_content_hash_tz.py --apply
```

worker 同一次运行（CLI 的 `--loop` 或站内 API 的 `loop`）里失败过的 id 会先跳过，避免对齐失败把队列钉死。

## 部署协调

`translate_article` 在整篇（包括多次内部提交）期间持数据库共享会话锁，覆盖 HTTP worker 与开发排障 CLI。自动部署拿不到独占锁就等下一轮，不终止正在译的文章。部署已持锁时新请求的任务记为 `last_status: deferred`，不计模型失败；loop 等待重试，单次任务由调用方稍后重试。空闲 loop 不阻挡部署，重启后的续跑仍须按上面的站内 POST 显式触发；本次没有自动开启翻译。首次部署交接及备份前提见 [deploy-safety.md](deploy-safety.md)。

# 站内 Grok 译制（中英对照）

英文原文入库后，由 **investment / research 站内** 用 Grok OAuth 翻译，排版对齐 AIHOT 文章页：

- 默认 **对照**：每段英文在上（小一号、次要色 `--ink-4`），中文在下
- Tab：**对照 / 中文 / 原文**（`/articles/{id}`、`/zh`、`/original`）
- 媒体与图片只在英文侧出现一次
- 模型固定 `grok-4.7`，`reasoning_effort=xhigh`，上下文约 50 万 token，**不用 fast**

不要站外译完再贴进库。译文表是 `article_translations`，按 `content_hash` 与原文对齐。

## 和 AIHOT 的关系

| | AIHOT | research |
|---|---|---|
| OAuth | `aihot_data` 卷 `/data/secrets/xai-oauth.json` | **同一文件**（挂载为 `/aihot-data/secrets/xai-oauth.json`） |
| 登录 | `node scripts/xai-login.ts login`（主入口） | `research-xai-login status` 检查即可；一般**不必**再 login |
| 调用 | worker `translatePending` / 读者点译 | `research-translate`（可 `--loop`） |
| 展示 | `.bilingual-en` / `.bilingual-zh` | 同名 class，同 token |

Mac mini 上 research 的 `docker-compose.yml` 把外部卷 `aihot_data` 挂到 `/aihot-data`，并用环境变量：

- `XAI_OAUTH_TOKEN_PATH=/aihot-data/secrets/xai-oauth.json`
- `XAI_OAUTH_FILE_UID=1000` / `XAI_OAUTH_FILE_GID=1000`（写入后 chown，保证 AIHOT 的 `node` 用户仍能刷新）

双方共用**同一路径**和同一 `.lock`，轮流刷新；不要再复制一份 token。Hermes 的 `auth.json` 格式会被拒绝。

`research-xai-login logout` 会删掉共用文件，AIHOT 也会退出，慎用。

## 登录（Mac mini）

AIHOT 已登录时，research **无需再授权**：

```bash
cd ~/research
docker compose exec app research-xai-login status
# 应显示已登录，token 路径在 /aihot-data/secrets/xai-oauth.json
```

若两边都未登录，在 AIHOT 仓库完成一次即可：

```bash
cd ~/aihot
docker compose exec api node scripts/xai-login.ts login
```

## 译制

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
3. `research-translate --source stratechery --loop`

游客半截 / guest teaser **不入库、不译**。

## 何时开跑翻译

| 状态 | 动作 |
|---|---|
| 译制管道已合入、AIHOT Grok 已 login | 可对库内已有合格英文稿 `research-translate` |
| Stratechery 仍在抓 | **先不 ingest**；可用英文样例验对照页 |
| 抓完并 ingest 后 | 对该来源 `--loop` 批量译 |

## 环境变量

见 `.env.example`：`XAI_OAUTH_TOKEN_PATH`、`XAI_OAUTH_FILE_UID`、`XAI_OAUTH_FILE_GID`、`XAI_BASE_URL`、`XAI_MAX_TOKENS`、`LLM_LONG_TIMEOUT_MS`。

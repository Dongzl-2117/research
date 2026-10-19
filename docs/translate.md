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
| OAuth | 自有 `data/secrets/xai-oauth.json` | **自有** `$DATA_DIR/secrets/xai-oauth.json` |
| 登录 | `node scripts/xai-login.ts login` | `research-xai-login login` |
| 调用 | worker `translatePending` / 读者点译 | `research-translate`（可 `--loop`） |
| 展示 | `.bilingual-en` / `.bilingual-zh` | 同名 class，同 token |

**禁止**把 AIHOT 的 token 文件指给 research（或反过来）。xAI 每次 refresh 都会换发 refresh token，共用会两边一起登出。

## 登录（Mac mini）

```bash
cd ~/research
docker compose exec app research-xai-login login
# 或本机：research-xai-login login
```

终端打印验证地址和 `user_code`，在本机浏览器用有 Grok 订阅的账号授权。
`research-xai-login status` / `logout`。

文件权限：目录 `700`，token `600`。已在 `data/` 下，不进 Git。可用 `XAI_OAUTH_TOKEN_PATH` 改路径。

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
| 译制管道已合入、Grok 已 login | 可对库内已有合格英文稿 `research-translate` |
| Stratechery 仍在抓 | **先不 ingest**；管道可先登录、用英文样例验对照页 |
| 抓完并 ingest 后 | 对该来源 `--loop` 批量译 |

## 环境变量

见 `.env.example`：`XAI_OAUTH_TOKEN_PATH`、`XAI_BASE_URL`、`XAI_MAX_TOKENS`、`LLM_LONG_TIMEOUT_MS`。

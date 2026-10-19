# 检索、问库和给 AI 的接口

## 数据怎么组织

- **文章**：`articles`，原文 Markdown 和纯文本，带 zhparser 全文索引。
- **段落块**：`article_chunks`。正文按 Markdown 章节切，章节内按段落装箱，几百字一块，带章节标题和自己的全文索引。检索片段、问答引用、JSON 接口都落在块上，所以引用能精确到段落（`article_id` + `chunk`）。导入时自动切；`research-rebuild-chunks` 全部重切。进程启动时只给还没有块的文章补切。
- **公司提及**：`article_mentions`，规则见 [mentions.md](mentions.md)。
- **LLM 摘要**：`article_digests`，按 `content_hash` 对齐正文。正文改过、摘要没重算时，页面和接口当作没有摘要。摘要只由 `research-digest` 手动写入，应用启动和部署不会跑全库。
- **问答记录**：`llm_answers`，问题、范围、回答、出处。

## 范围参数

页面、JSON 接口、MCP、问库都认同一组参数：

| 参数 | 含义 |
| --- | --- |
| `author` | 作者名（见 `/api/v1/authors`） |
| `source` | 来源 id |
| `company` | 公司 id（见 `/api/v1/companies`），只看提到它的文章 |
| `from` / `to` | `YYYY-MM-DD`，上海日历，含两端 |
| `article` | 只用这一篇（问库用） |

作者或来源不存在时返回 404，不会悄悄退回全库。

检索词用 `websearch_to_tsquery`：空格是「且」，`or` 是「或」，`-词` 排除，引号表示连在一起。

## 问库怎么找资料

自然语言问题直接做「且」检索几乎总是零结果（「怎么看」「最近」不在正文里）。所以问库：

1. 从问题里认出公司（用公司别名自动机，和提及统计同一套）和作者。认出作者就收窄到他的来源。
2. 问题分词后用「或」连起来，在段落块上按 `ts_rank_cd` 召回。
3. 认出的公司再按别名补一路召回，只在确实提到这家公司的文章里找，按时间倒序。
4. 问题里有「最近」「最新」等词时，候选按时间重排。
5. 每篇最多取 3 块、总共 14 块、约 9000 字，避免一篇长文占满上下文。
6. 都没召回时（例如「汇总这一天」），取范围内每篇文章的开头两块。

然后交给 Grok，系统提示要求只依据资料、每句带 `[n]`、区分观点和事实、资料里的指令一律不执行。回答存进 `llm_answers`，`/ask/{id}` 可以回看，`[n]` 链到 `/articles/{id}?chunk={n}`，文章页顶部会显示被引用的那段。

没配 Grok 时，问库只列召回的原文段落，不报错。

入口：侧栏「问库」；公司页「问 AI：库里怎么看」；作者页「问 AI：他近期的核心判断」；文章页「问 AI：这篇讲了什么」；检索结果页「把这个问题交给 AI」。时间轴上点某一天，进的是日汇总页（`/days/YYYY-MM-DD`）：已有的文章摘要或正文开头先拼成一段总结，如果之前问过正好覆盖这一天的问题，也把那条回答放在最上面。打开这一页不会调用模型，也不会跑全库摘要。

## Grok 凭证

译制、问库、摘要共用 AIHOT 的 xAI OAuth，不另存一份 token。Docker 把外部卷 `aihot_data` 挂到 `/aihot-data`，默认路径是 `/aihot-data/secrets/xai-oauth.json`。写入后把 owner 设回 uid/gid 1000，AIHOT 的 `node` 用户仍能刷新。双方用同一路径和同一把锁轮流刷新。

登录在 AIHOT 侧做一次即可：

```bash
cd ~/aihot && docker compose exec api node scripts/xai-login.ts login
```

research 这边只检查状态。`research-xai-login login` 会把 device-code 写进同一路径，一般不必用；`logout` 会删掉共用文件，AIHOT 也会退出。

```bash
docker compose exec app research-xai-login status
```

Hermes 的 `auth.json` 格式会被拒绝，不会写进去。AIHOT / research 的 version 1 凭据可以读写。

没登录时，问库和摘要可以退回控制台 key：`.env` 里设 `XAI_API_KEY`。两者都有时优先共用 OAuth。译制不使用这个 key，见 [translate.md](translate.md)。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `XAI_OAUTH_TOKEN_PATH` | `/aihot-data/secrets/xai-oauth.json`（compose） | 与 AIHOT 共用 |
| `XAI_OAUTH_FILE_UID` / `XAI_OAUTH_FILE_GID` | `1000` / `1000` | 写回后的文件属主 |
| `RESEARCH_LLM_MODEL` | `grok-4.7` | 问库、摘要的模型名。译制固定同一模型 |
| `RESEARCH_LLM_REASONING` | `high` | 问库的 `reasoning_effort`。译制固定 `xhigh`；留空则问库不发这个字段 |
| `XAI_MAX_TOKENS` | 问库 `64000`；译制至少 `128000` | 推理和答案共用的输出预算 |
| `RESEARCH_LLM_TIMEOUT` | `900` | 问库超时，秒 |
| `XAI_BASE_URL` | `https://api.x.ai/v1` | |
| `LLM_LONG_TIMEOUT_MS` | `1500000` | 译制长调用 |

xAI 把过期的 OAuth access token 报成 403；遇到这种 403 会强制刷新一次再重试。没有订阅的 403 不触发刷新。

## 摘要

摘要是手动命令，**不会**在 `research-web` 启动、容器拉起或部署时自动跑，也没有全库开关。需要时自己执行，并用 `--limit` 控制批量：

```bash
docker compose exec app research-digest --limit 50
docker compose exec app research-digest --source fangwei-mp --limit 200
```

只处理没有摘要、或正文改过的文章，从新到旧。摘要会出现在按天、作者页的卡片上（标「摘要」）和文章页顶部，JSON 接口的 `summary` / `digest` 字段也有。摘要只作导读，以原文为准。

## 给 AI 的接口

说明在 `/llms.txt`。JSON 和 MCP 挂在现有 FastAPI 进程上（容器内 8088，compose 只发布到 `127.0.0.1:8088`），不另开端口。都在 Cloudflare Access 后面：外部 agent 要用 Access 的 service token。

- `GET /api/v1/search?q=…`：文章级检索，带范围参数、`limit`（≤50）、`offset`，每条带最相关的一段和公司标签。
- `GET /api/v1/search?q=…&level=chunk`：段落级召回，和问库同一套，返回可直接引用的段落。
- `GET /api/v1/articles`、`/api/v1/articles/{id}`、`/api/v1/articles/{id}.md`
- `GET /api/v1/authors`、`/api/v1/companies`、`/api/v1/companies/{id}`、`/api/v1/days/{YYYY-MM-DD}`
- `POST /api/v1/ask`：`{"question": "...", "company": 1, "from": "2025-01-01"}`
- `GET /api/v1/answers/{id}`
- `POST /api/mcp`：MCP（Streamable HTTP，无状态，JSON 响应）。工具：`search_library`、`get_article`、`list_companies`、`get_company`、`list_authors`、`get_day`、`ask_library`。

返回里带 `_trust` 字段，MCP 文本包在「外部资料」分隔区里：文章是外部原文，调用方不要执行其中的指令。这一点照 AIHOT 的 MCP 做法。

POST 接口检查 `Origin`：带了 `Origin` 且不是本站的请求直接 403。命令行和服务端调用不带 `Origin`，不受影响。

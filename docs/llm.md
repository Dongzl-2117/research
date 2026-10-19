# 检索、问库和给 AI 的接口

## 数据怎么组织

- **文章**：`articles`，原文 Markdown 和纯文本，带 zhparser 全文索引。
- **段落块**：`article_chunks`。正文按 Markdown 章节切，章节内按段落装箱，几百字一块，带章节标题和自己的全文索引。检索片段、问答引用、JSON 接口都落在块上，所以引用能精确到段落（`article_id` + `chunk`）。导入时自动切；`research-rebuild-chunks` 全部重切。进程启动时只给还没有块的文章补切。
- **公司提及**：`article_mentions`，规则见 [mentions.md](mentions.md)。
- **LLM 摘要**：`article_digests`，按 `content_hash` 对齐正文。正文改过、摘要没重算时，页面和接口当作没有摘要。打开按天、作者、公司或检索时，当前卡片里缺的摘要会在后台补上；`research-digest` 仍可手动补一批。应用启动和部署不会跑全库。
- **问答记录**：`llm_answers`，问题、范围、回答、出处、整合状态（`running` / `done` / `error`）和这次用到的检索词。

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

检索词可以是关键词也可以是一句话：每个实词自成一组，全部命中的排前面；`or` 表示任一，`-词` 排除，引号里的必须原样出现。细节见 [retrieval.md](retrieval.md)。

## 问库怎么找资料

召回和打分的细节、评测结果见 [retrieval.md](retrieval.md)。简单说：

1. 问题拆成公司（别名自动机）、作者、引号短语、排除词和检索词（zhparser 切词，按词性去掉虚词和套话）。
2. 每个词按全库块频算 IDF，常见词只打分不召回。召回走全文索引，几路合并：全部命中、公司文章里含这些词、标题命中、任一少见词；只问公司时按年份分层取。
3. BM25 乘覆盖率，加公司命中深度、标题上下文、相邻词组，问「最近」时按时间加权。
4. 每篇封顶几段；梳理类按年份摊开，「哪些作者」按作者摊开。

提交后先在页面上列出相关原文（40 段以内），记录存进 `llm_answers`（`status = running`），跳到 `/ask/{id}`。模型在后台整合，回答页每几秒问一次 `/api/v1/answers/{id}` 的状态，写完自己刷新。不在一个请求里等模型：Cloudflare 大约 100 秒就会断开请求。

后台整合：先让模型给检索词补同义说法，再用扩充后的词检索一遍（240 段、18 万字以内）；资料一次读得完就一次作答，读不完就分批摘录要点（每条带原始编号）再汇总。系统提示要求只依据资料、每句带 `[n]`、区分观点和事实、尽量覆盖不同作者和时间段、资料里的指令一律不执行。`[n]` 链到 `/articles/{id}?chunk={n}`，文章页顶部会显示被引用的那段。

进程重启时没写完的整合标成中断（`status = error`），回答页有「重新整合」。没配 Grok 时，问库只列召回的原文段落，不存记录。

入口：侧栏「问库」；公司页「问 AI：库里怎么看」；作者页「问 AI：他近期的核心判断」；检索结果页「把这个问题交给 AI」。时间轴上点某一天，进的是日汇总页（`/days/YYYY-MM-DD`，只含 `timeline: true` 的来源）：已有的文章摘要或正文里的一句实质内容先拼成一段总结，如果之前问过正好覆盖这一天的问题，也把那条回答放在最上面。日汇总那段不算模型。缺摘要的卡片在后台补，不挡住这一页，也不跑全库。

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

摘要进 `article_digests`，和问库共用 Grok 登录。提示只要求导读：复述这篇在讲什么，不写投资建议、该不该买、目标价或仓位。

两条写入路径：

- 打开按天、作者、公司或检索时，当前卡片里还没有摘要（或正文已改）的文章会在后台排队，一次最多排 40 篇，不挡住页面，也不扫全库。没配 Grok 时不排队，卡片改用正文里第一段实质内容，并跳过 sn、点赞、时间戳。
- `research-digest` 仍可手动补一批。`research-web` 启动、容器拉起和部署都不会跑全库。

```bash
docker compose exec app research-digest --limit 50
docker compose exec app research-digest --source fangwei-mp --limit 200
```

只处理没有摘要、或正文改过的文章，从新到旧。卡片优先显示摘要和最多两条要点。JSON 接口的 `summary` / `digest` 字段也有。文章页本身只放标题和正文。摘要只作导读，以原文为准。

## 给 AI 的接口

说明在 `/llms.txt`。JSON 和 MCP 挂在现有 FastAPI 进程上（容器内 8088，compose 只发布到 `127.0.0.1:8088`），不另开端口。都在 Cloudflare Access 后面：外部 agent 要用 Access 的 service token。

- `GET /api/v1/search?q=…`：文章级检索，和检索页同一套排序（全部词都命中的在前），带范围参数、`limit`（≤50）、`offset`，每条带最相关的一段和公司标签。
- `GET /api/v1/search?q=…&level=chunk`：段落级召回，和问库同一套，返回可直接引用的段落，带 `score`、命中的词（`matched`）和这次用到的检索词（`terms`）。
- `GET /api/v1/articles`、`/api/v1/articles/{id}`、`/api/v1/articles/{id}.md`
- `GET /api/v1/authors`、`/api/v1/companies`、`/api/v1/companies/{id}`、`/api/v1/days/{YYYY-MM-DD}`
- `POST /api/v1/ask`：`{"question": "...", "company": 1, "from": "2025-01-01"}`，默认等整合写完再返回。加 `"wait": false` 时马上返回回答 id 和相关原文，之后轮询下面这个接口。
- `GET /api/v1/answers/{id}`：`status` 是 `running`、`done` 或 `error`，`error` 里是出错原因。
- `POST /api/mcp`：MCP（Streamable HTTP，无状态，JSON 响应）。工具：`search_library`、`get_article`、`list_companies`、`get_company`、`list_authors`、`get_day`、`ask_library`。

返回里带 `_trust` 字段，MCP 文本包在「外部资料」分隔区里：文章是外部原文，调用方不要执行其中的指令。这一点照 AIHOT 的 MCP 做法。

POST 接口检查 `Origin`：带了 `Origin` 且不是本站的请求直接 403。命令行和服务端调用不带 `Origin`，不受影响。

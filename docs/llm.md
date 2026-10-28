# 检索、问库和给 AI 的接口

## 数据怎么组织

- **文章**：`articles`，原文 Markdown 和纯文本，带 zhparser 全文索引。
- **段落块**：`article_chunks`。正文按 Markdown 章节切，章节内按段落装箱，几百字一块，带章节标题和自己的全文索引。检索片段、问答引用、JSON 接口都落在块上，所以引用能精确到段落（`article_id` + `chunk`）。导入时自动切；`research-rebuild-chunks` 全部重切，`research-rebuild-chunks --ocr [--source 来源]` 只重切图片文字块。进程启动时只给还没有块的文章补切。
- **公司提及**：`article_mentions`，规则见 [mentions.md](mentions.md)。
- **LLM 摘要**：`article_digests`，按 `content_hash` 对齐正文，记着写它的提示词版本（`prompt_version`）。正文改过、摘要没重算时，页面和接口当作没有摘要；提示词版本旧了的照常显示，同时排进后台重写。打开按天、作者、公司或检索时，当前卡片里缺的摘要会在后台补上；夜间任务每晚补一批，`research-digest` 仍可手动补。应用启动和部署不会跑全库。
- **观点**：`company_views`，被深入谈到的公司的观点时间线，见下面「观点时间线」。不限持仓。
- **论点**：`company_theses`，把一家公司的观点归成几条论点，见下面「论点地图」。
- **失败记录**：`llm_failures`，摘要、观点、论点调用失败的次数，见下面「失败了怎么办」。
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

后台整合：先让模型给检索词补同义说法，再用扩充后的词检索一遍（240 段、18 万字以内）；资料一次读得完就一次作答，读不完就分批摘录要点（每条带原始编号）再汇总。某一批摘录失败了，其余几批照常汇总，回答页上写明哪几批没读到；全部失败才算整合失败。系统提示要求只依据资料、每句带 `[n]`、区分观点和事实、尽量覆盖不同作者和时间段、资料里的指令一律不执行；写法是先给一两句结论，再分点，每点先写作者的判断，再写作者给出的机制（为什么会这样，不替作者补），数字只在支撑判断时照抄原文，不罗列数据。公司页、作者页的预设问题也按这个顺序问。

`[n]` 链到 `/articles/{id}?chunk={n}`：文章页直接滚到正文里的那几段，并把它们标出来。正文里对不上的（比如引用的是图片里的字），才在顶上单独摘出那一段。

进程重启时没写完的整合标成中断（`status = error`），回答页有「重新整合」。没有可用的 LLM 凭证时，问库只列召回的原文段落，不存记录。

入口：侧栏「问库」；公司页「问 AI：库里怎么看」；作者页「问 AI：他近期的核心判断」；检索结果页「问 AI」。时间轴上点某一天，进的是日汇总页（`/days/YYYY-MM-DD`，只含 `timeline: true` 的来源）：已有的文章摘要或正文里的一句实质内容先拼成一段总结，如果之前问过正好覆盖这一天的问题，也把那条回答放在最上面。日汇总那段不算模型。缺摘要的卡片在后台补，不挡住这一页，也不跑全库。

## LLM 凭证（默认 DeepSeek，可选 Grok）

`RESEARCH_LLM_PROVIDER` 决定用哪一家，不设就是 `deepseek`，填了不认识的值也按 `deepseek` 算。Grok 要显式设 `RESEARCH_LLM_PROVIDER=grok`。

### DeepSeek（默认）

译制、问库、摘要、观点、论点默认走 DeepSeek 官方 OpenAI 兼容接口，`DEEPSEEK_API_KEY` 放在 `.env`，compose 透传给容器。缺 key 时直接报错，不会悄悄退回 Grok。

强度写在 `researchlib/deepseek.py`，译制（`translate/llm.py` 的 `chat_json`）和问库、摘要、观点、论点、样卡（`researchlib.llm.chat`）共用同一组常量：

| 项 | 值 |
| --- | --- |
| 模型 | `deepseek-flash`（`DEEPSEEK_MODEL`）。默认 V4.1-Flash |
| thinking | `{"type": "enabled"}`，默认打开 |
| `reasoning_effort` | `max`（`DEEPSEEK_REASONING_EFFORT`，只认 `low` / `high` / `max`；`xhigh` 按 `high`） |
| 输出预算 | 至少 `393216`（`DEEPSEEK_OUTPUT_FLOOR`）。推理和答案共用这笔预算 |
| 上下文 | `1000000` token（`DEEPSEEK_CONTEXT_TOKENS`） |
| 不发的字段 | `temperature`、`presence_penalty`、`frequency_penalty`：思考模式下无效，请求里不带 |

最终答案在 `choices[0].message.content`；`reasoning_content` 里的思考过程不往页面上放。

### Grok OAuth（可选）

设 `RESEARCH_LLM_PROVIDER=grok` 才走这里。译制、问库、摘要使用 research 自有 `data/secrets/xai-oauth.json`（容器内 `/data/secrets/xai-oauth.json`）。AIHOT 已下线，不再共用外部卷。

登录在 AIHOT 侧做一次即可：

```bash
cd ~/aihot && docker compose exec api node scripts/xai-login.ts login
```

research 这边只检查状态。`research-xai-login login` 会把 device-code 写进同一路径，一般不必用；`logout` 会删掉共用文件，AIHOT 也会退出。

```bash
docker compose exec app research-xai-login status
```

Hermes 的 `auth.json` 格式会被拒绝，不会写进去。AIHOT / research 的 version 1 凭据可以读写。

Grok 路径只认这一份 OAuth。文件不在、未登录或刷新失败时直接报错，不退回 `XAI_API_KEY`。环境里如果还留着 `XAI_API_KEY`，调用会说明这个变量不会被使用。

强度写在 `researchlib/grok.py`，和 DeepSeek 一样由译制与问库、摘要、观点、论点、样卡共用：

| 项 | 值 |
| --- | --- |
| 模型 | `grok-4.7`（`GROK_MODEL_ID`）。不读 `RESEARCH_LLM_MODEL`，请求里不发 `fast` |
| `reasoning_effort` | `xhigh`（`GROK_REASONING_EFFORT`）。不读 `RESEARCH_LLM_REASONING` |
| 输出预算 | 至少 `128000`（`GROK_OUTPUT_FLOOR`）。推理和答案共用这笔预算 |
| 上下文 | `500000` token（`GROK_CONTEXT_TOKENS`），用 grok-4.7 自带的窗口，不另传更小上限 |

xAI 把过期的 OAuth access token 报成 403；遇到这种 403 会强制刷新一次再重试。没有订阅的 403 不触发刷新。

### 变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RESEARCH_LLM_PROVIDER` | `deepseek` | `deepseek` / `grok` |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 必填，缺了直接报错 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 不另加 `/v1` |
| `DEEPSEEK_MODEL` | `deepseek-flash` | V4.1-Flash |
| `DEEPSEEK_REASONING_EFFORT` | `max` | `low` / `high` / `max` |
| `DEEPSEEK_MAX_TOKENS` / `LLM_MAX_TOKENS` | 不设则用 `393216` | 只能把输出预算抬得更高，设得更低也会回到 `393216` |
| `XAI_OAUTH_TOKEN_PATH` | `/data/secrets/xai-oauth.json`（compose） | 仅 Grok，与 AIHOT 共用 |
| `XAI_OAUTH_FILE_UID` / `XAI_OAUTH_FILE_GID` | `0` / `0` | 仅 Grok，写回后的文件属主 |
| `XAI_MAX_TOKENS` | 不设则用 `128000` | 仅 Grok，只能抬高 |
| `XAI_BASE_URL` | `https://api.x.ai/v1` | 仅 Grok |
| `LLM_LONG_TIMEOUT_MS` | `1500000` | 译制和问库共用的长超时，默认 25 分钟 |
| `RESEARCH_LLM_TIMEOUT` | 不设 | 秒。比上面的长超时更短时会被抬到同一条，不能单独把问库调弱 |
| `RESEARCH_LLM_CONCURRENCY` | `3` | 同一个进程里同时在跑的 LLM 请求上限，问库分批、摘要、观点、论点共用 |

429、5xx、连接被重置这类临时错误退避重试两次（等 5 秒、20 秒；接口给了 `Retry-After` 就按它，最多等一分钟）。超时不重试：一次已经等了很久。凭证不可用或连续失败 3 次，首页顶上提示一行，写明是 DeepSeek 还是 Grok（见 [backup.md](backup.md)「出问题才提示」）。

## 摘要

摘要进 `article_digests`，和问库共用同一份 LLM 凭证。提示只要求导读，不写投资建议、该不该买、目标价或仓位。要点先写判断，再写作者给出的机制；数字只在支撑判断时引用；原文谈到风险、推翻条件或验证指标才写，没谈到就不写。提示词改了就把 `digest.PROMPT_VERSION` 加一，旧摘要照常显示，被看到或跑 `research-digest` 时按新版重写（没有摘要的排在只是版本旧了的前面）。

两条写入路径：

- 打开按天、作者、公司或检索时，当前卡片里还没有摘要（或正文已改）的文章会在后台排队，一次最多排 40 篇，不挡住页面，也不扫全库。没有可用的 LLM 凭证时不排队，卡片改用正文里第一段实质内容，并跳过 sn、点赞、时间戳。
- `research-digest` 仍可手动补一批。`research-web` 启动、容器拉起和部署都不会跑全库。

```bash
docker compose exec app research-digest --limit 50
docker compose exec app research-digest --source fangwei-mp --limit 200

站内正式触发（生产续跑走这里，不要另起 CLI loop）：

```bash
curl -s http://127.0.0.1:8088/api/v1/digest -H 'content-type: application/json' \
  -d '{"limit":20,"loop":true,"sleep":60}'
curl -s http://127.0.0.1:8088/api/v1/digest
curl -s -X POST http://127.0.0.1:8088/api/v1/digest/stop
```

```

只处理没有摘要、正文改过、提示词版本旧了、或当时只读了长文开头的文章，从新到旧。卡片优先显示摘要和最多两条要点。JSON 接口的 `summary` / `digest` 字段也有。文章页本身只放标题和正文。摘要只作导读，以原文为准。

### 长文读哪些

以前摘要只给模型正文前 1.2 万字、观点只给前 3 万字，长文的结论和估值常在后半段，后半段才谈到的公司会被记成「没下判断」。现在（`src/researchlib/excerpt.py`）：

- 摘要：全文不超过 6.4 万字（和问库一次读的量一样）就给全文；更长的取开头（四成）、结尾（两成）和每一节的第一块，剩下的名额从中间均匀补，按原文顺序拼起来，跳过的地方用省略号隔开，并告诉模型这是删节过的。
- 观点：全文不超过 3 万字就给全文；更长的只给提到这几家公司的段落（和它后面一段），加上开头两段和最后一段。

以前只读了开头写出来的摘要和观点（迁移时按正文长度标出 `head_only`）照常显示，被看到时、夜间任务里按新的读法重写。

### 失败了怎么办

摘要、观点、论点的调用失败了记进 `llm_failures`：失败 n 次就隔 n 天再试（最多一周），期间打开页面不再排队，免得接口抽风的那几篇每次被打开都重新花一次推理。写成功就清掉。批量命令（`research-digest`、`research-views`、`research-theses`）个别失败照常跑完，退出码 0；没有可用的 LLM 凭证时退出码 2（`research-pilot` 同样）。不会在两个提供方之间自动切换。

## 观点时间线

被深入谈到的公司，公司页上都有「观点」（不限 `config/portfolio.yaml`）：每篇深入谈到它的文章抽一条作者对这家公司的判断。

- 每条：作者的判断、作者给出的机制、态度（看好 / 看淡 / 两面 / 中性）。风险、推翻条件、要盯的验证指标只在原文谈到时才有，没谈到就空着。
- 原句逐字核对过（只比汉字、字母、数字）才显示，并链到它所在的段落；对不上就不显示，免得把模型的转述当成原话。
- 判断或态度为空白的行不进时间线，也不进论点。那是「没下判断」的缓存，避免反复问模型。确认是抽漏了，用 `--refresh-blank` 删掉再抽；夜间任务不要加这个参数。
- 筛选：全部、风险与反方（有风险或推翻条件、或态度是看淡、两面）、态度变化（同一位作者这篇的态度和他上一篇不同；中性不算信号）。点作者名只看这位作者。
- 一篇文章里深入谈到的几家公司一次整理，不为每家公司各问一次模型。按正文 `content_hash` 和 `views.PROMPT_VERSION` 缓存，正文或提示词变了才重抽。

两条写入路径，和摘要一样：打开公司页时，这家公司最近还没整理的文章在后台排队（一次最多 20 篇），页面上写「还有 N 篇在整理」；夜间任务每晚补一批；也可以手动批量补：

```bash
docker compose exec app research-views                       # 全部被深入谈到的公司，最多 100 篇
docker compose exec app research-views --company 泡泡玛特 --limit 300
docker compose exec app research-views --company 贵州茅台 --refresh-blank
# 有提及但没有可用观点的都补（标题级命中、空白缓存、去重后留下的主版本）。夜间任务不要加 --gap
docker compose exec app research-views --gap --company 贵州茅台 --company 泡泡玛特 --limit 800
docker compose exec app research-views --gap --source fangwei-zsxq --limit 400
# 段永平博客先回扫再抽。只跑这个来源，不碰别的作者，也不要在没回扫时全量抽。
docker compose exec app research-views --source duanyongping-blog --limit 100
docker compose exec app research-views --articles 1408,1324,8636,350,11583,13516,2052,11299

站内正式触发：

```bash
curl -s http://127.0.0.1:8088/api/v1/views -H 'content-type: application/json' \
  -d '{"gap":true,"limit":20,"loop":true,"sleep":60}'
curl -s http://127.0.0.1:8088/api/v1/views
curl -s -X POST http://127.0.0.1:8088/api/v1/views/stop
```

```

`--gap` 的范围：`article_mentions` 里深度不是「一带而过」、又没有一条 about 为真且判断非空的观点。去重后的主版本（有别的稿 `duplicate_of` 指向它）即使提及只是一带而过也补抽，例如 3851 并入 11299 之后要抽 11299。网友帖不抽。已经记成 about=false（论证不在这家公司）的不反复问。

超长文档先按段落切块，每块单独问，再合成：同一家公司的不同判断留在 `angles`，态度相反记成两面。论证没有落在这家公司时 `about=false`，不进时间线，也不进论点。一篇可以同时写几家公司，每家用自己的角度。

问库、观点、摘要、论点都只用上面这一份 OAuth，强度与译制相同，不退回 API key。

应用启动和部署不会跑。方伟星球用 `research-retime` 把零点改成真实 `create_time` 之后，态度变化和「同期别人怎么看」会按新的 `published_at` 重算（见 [ingest.md](ingest.md)），不必为此再跑 `research-views`。JSON：`GET /api/v1/companies/{id}/views`（`views=risk`、`views=change`、`views=t1`、`author=`）。

观点还用在两处：

- 首页「最近观点」：有判断的公司按最近一条观点的时间排（不限持仓，最多 8 家）。收起时一行公司名；展开后只显示这篇已有的导读，没有导读就不铺判断原文，也不在打开页面时另调模型。旁边是态度和近 30 天深入谈到它的篇数。
- 文章页底部「同期别人怎么看」：这篇深入谈到的公司（不限持仓），别的作者前后 30 天里的判断，离这篇越近的越靠前，最多 6 条，同一位作者的不列。

## 论点地图

观点时间线攒到几百条以后，按时间排看不出结构。观点够数（至少 4 条）的公司，页上在观点上面有「论点」（不限持仓）：把这家公司的观点交给模型按机制归成几条（通常 2 到 5 条，最多 6 条），每条引用具体的观点。

模型只负责归类。写进页面之前还有一层校验，不单靠模型：

- 每条论点除了标题和机制，还存判断、证据链、时间变化、分歧，以及可点回文章的来源（文章 id）。
- 支持和反对看这条判断同不同意**这条论点**，不看作者对整家公司是看好还是看淡。看好公司的人说「现阶段不宜提价」，进反对。说不清、又没有模型给出的边，就丢掉，不退回总态度。
- 原句为空、只有换行、或规范化之后对不上原文的引用拒收，不进这条论点。比对前去掉空白、换行和全角差异。
- 列举、顺带、举例（about=false，或判断本身是名单）不进支持。
- 公众号和星球转贴：判断或原句规范化后互相包含的只留一份（有原句的优先）。
- 引用集合高度重叠的论点合并。同一条观点挂到多条论点上时，匹配分明显更高的那条留下；分不出高下才最多挂两条。
- 判断对不上这条论点的标题和机制，这条引用丢掉。原句也对不上时，这条论点下面不显示那句原句。
- 只有一篇、或只有一位作者又没有分歧的论点丢掉。没被收进任何论点的观点仍在「全部」时间线里。
- 判断或态度为空白、以及网友帖的观点不进这次输入。
- 谁支持、谁反对、各几篇、最近一次是谁哪天说的，都按引用的观点从库里数。点一条论点，下面的时间线只看这几篇（`?views=t1`）。
- 观点不到 4 条的公司不做。一家公司一次调用；观点有变化才重算，一天最多一次（按观点集合的指纹和提示词版本缓存）。提示词版本变了，已存的地图也会过期。
- 打开公司页默认不在后台补（`RESEARCH_THESES_AUTO` 默认 0）。夜间默认也不跑（`THESES_ENABLED` 默认 0）。打开之后范围是观点够数的公司，不写死持仓。启动和部署不跑。

```bash
docker compose exec app research-theses                        # 观点够数的公司，过期的才算
docker compose exec app research-views --company 贵州茅台 --company 泡泡玛特 --refresh-blank --limit 300
docker compose exec app research-theses --company 贵州茅台 --company 泡泡玛特 --force

站内正式触发：

```bash
curl -s http://127.0.0.1:8088/api/v1/theses -H 'content-type: application/json' \
  -d '{"loop":true,"sleep":120}'
curl -s http://127.0.0.1:8088/api/v1/theses
curl -s -X POST http://127.0.0.1:8088/api/v1/theses/stop
```

```

JSON 和 MCP 的观点接口里都带 `theses`。

## 给 AI 的接口

说明在 `/llms.txt`。JSON 和 MCP 挂在现有 FastAPI 进程上（容器内 8088，compose 只发布到 `127.0.0.1:8088`），不另开端口。都在 Cloudflare Access 后面：外部 agent 要用 Access 的 service token。

- `GET /api/v1/search?q=…`：文章级检索，和检索页同一套排序（全部词都命中的在前），带范围参数、`limit`（≤50）、`offset`，每条带最相关的一段和公司标签。
- `GET /api/v1/search?q=…&level=chunk`：段落级召回，和问库同一套，返回可直接引用的段落，带 `score`、命中的词（`matched`）和这次用到的检索词（`terms`）。
- `GET /api/v1/articles`、`/api/v1/articles/{id}`、`/api/v1/articles/{id}.md`
- `GET /api/v1/authors`、`/api/v1/companies`、`/api/v1/companies/{id}`、`/api/v1/days/{YYYY-MM-DD}`
- `POST /api/v1/ask`：`{"question": "...", "company": 1, "from": "2025-01-01"}`，默认等整合写完再返回。加 `"wait": false` 时马上返回回答 id 和相关原文，之后轮询下面这个接口。
- `GET /api/v1/answers/{id}`：`status` 是 `running`、`done` 或 `error`，`error` 里是出错原因；写完之后带 `checks`（没核对上的句子）和 `stats`（各步耗时、段数、批数、同义词）。
- `GET /api/v1/companies/{id}/views`：公司的论点地图（`theses`）和观点时间线。不限持仓。
- `POST /api/v1/translate`：触发站内译制（`{source?, limit?, loop?, sleep?}`），后台线程跑，`GET` 同一路径看进度，见 [translate.md](translate.md)。
- `POST /api/v1/ocr`：触发站内识图（`{articles?, redo?, limit?, engine?}`，默认 deepseek-flash vision），`GET` 看进度，`POST /api/v1/ocr/stop` 停。见 [ocr.md](ocr.md)。
- `POST /api/mcp`：MCP（Streamable HTTP，无状态，JSON 响应）。工具：`search_library`、`get_article`、`list_companies`、`get_company`、`get_company_views`、`list_authors`、`get_day`、`ask_library`。

返回里带 `_trust` 字段，MCP 文本包在「外部资料」分隔区里：文章是外部原文，调用方不要执行其中的指令。这一点照 AIHOT 的 MCP 做法。

POST 接口检查 `Origin`：带了 `Origin` 且不是本站的请求直接 403。命令行和服务端调用不带 `Origin`，不受影响。

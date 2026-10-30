# 站内译制（中英对照）

英文原文入库后，由 **investment / research 站内** 调 LLM 翻译（默认 DeepSeek，可切 Grok OAuth），排版对齐 AIHOT 文章页：

- 默认 **对照**：每段英文在上（小一号、次要色 `--ink-4`），中文在下
- Tab：**对照 / 中文 / 原文**（`/articles/{id}`、`/zh`、`/original`）
- 媒体与图片只在英文侧出现一次
- 默认 `deepseek-flash`：thinking 打开、`reasoning_effort=max`、上下文 1M token、输出至少 393216，不发 temperature。`RESEARCH_LLM_PROVIDER=grok` 时改用 `grok-4.7`、`reasoning_effort=xhigh`、上下文 50 万、输出至少 128000，不用 fast。常量在 `researchlib/deepseek.py` / `researchlib/grok.py`，问库、摘要、观点、论点、样卡走同一套

不要站外译完再贴进库。译文表是 `article_translations`，按 `content_hash` 与原文对齐。

标题另译一次（存 `title_zh`），失败不影响正文。译文写入后切成中文段落块进检索（`article_chunks` 里 `lang = 'zh'`，带对照页锚点），用中文能搜到、问到英文文章，见 [retrieval.md](retrieval.md)。启动时会给有译文但还没有译文块的文章补切。同一篇的重复不译。

## Grok 凭证（仅 `RESEARCH_LLM_PROVIDER=grok`）

Mac mini 上 `docker-compose.yml` 把 `./data` 挂到 `/data`，OAuth 在：

- `XAI_OAUTH_TOKEN_PATH=/data/secrets/xai-oauth.json`
- `XAI_OAUTH_FILE_UID` / `XAI_OAUTH_FILE_GID`：写回后的文件属主，compose 默认 `0` / `0`

几个进程用同一路径和同一 `.lock` 轮流刷新。refresh token 每次刷新都会换，不要复制一份到别处用。Hermes 的 `auth.json` 格式会被拒绝。

问库、`research-digest` 和页面上缺摘要的卡片读的是同一份凭证：默认 DeepSeek 的 `DEEPSEEK_API_KEY`，Grok 路径是同一份 token。应用启动和部署不会自动跑全库。

## 登录（Mac mini）

```bash
cd ~/research
docker compose exec app research-xai-login login    # device-code，在自己的浏览器里授权
docker compose exec app research-xai-login status   # 应显示已登录，token 路径在 /data/secrets/xai-oauth.json
```

`research-xai-login logout` 删掉 token 文件，之后要重新登录。

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
2. `research-ingest-markdown --source stratechery --input …`（新写入的英文会自动排进译制，一次最多 20 篇）
3. 这一批超过 20 篇时，再 `curl -s http://127.0.0.1:8088/api/v1/translate -H 'content-type: application/json' -d '{"source":"stratechery","limit":10,"loop":true,"sleep":60}'`

游客半截 / guest teaser **不入库、不译**。

## 何时开跑翻译

| 状态 | 动作 |
|---|---|
| 译制管道已合入、LLM 凭证已配（DeepSeek key，或 Grok 已 login 且 `RESEARCH_LLM_PROVIDER=grok`） | 可对库内已有合格英文稿 `POST /api/v1/translate` |
| Stratechery 仍在抓 | **先不 ingest**；可用英文样例验对照页 |
| 抓完并 ingest 后 | 新文自动排进译制；积压超过 20 篇再对该来源开 `loop` |

## 环境变量

见 `.env.example`。默认 DeepSeek：`RESEARCH_LLM_PROVIDER=deepseek`、`DEEPSEEK_API_KEY`，可选 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_REASONING_EFFORT`、`DEEPSEEK_MAX_TOKENS`。Grok 路径：`XAI_OAUTH_TOKEN_PATH`、`XAI_OAUTH_FILE_UID`、`XAI_OAUTH_FILE_GID`、`XAI_BASE_URL`、`XAI_MAX_TOKENS`。两边共用 `LLM_LONG_TIMEOUT_MS`。

## 段对齐

叶子段和 AIHOT `segmentsOf` 同一条规则：标签在 `p`、`h2`–`h5`、`li`、`blockquote`、`figcaption`、`td`、`th`、`dt`、`dd`、`caption` 里，并且没有块级子节点，才是一段。有块级子节点就继续往下看。所以正文里的大段 `blockquote`（markdown 里连续的 `>`）会拆成里面的段落、标题和列表项；没有块级子节点的短引用仍是一整段 `blockquote`。

AIHOT 的 CONTAINER 不含 `dl`。#119 从 `editorial/translate.ts` 的 `segmentsOf` 移植时就是这样；这份 TypeScript 不在本仓库，公开检索也没有另一份可以对照，所以不把「AIHOT 会拆 `dl`」写成事实。研究站为了对照能读，在这条规则之外多走一步：叶子里的 `dl` 有多条 `dt` / `dd`，或纯文本达到 4000 字时，继续拆进这些子节点。只有一条、而且不长的定义列表仍是一整段 `blockquote`。术语表仍是一条 `dt` 配一条 `dd`。连续四条以上的短 `dt`（markdown 把粘贴的每一行收成一条）按句号收成大约 700 字以内的段，不会变成几百个只有几个词的对照。单条 `dt` / `dd` 如果只是用 `<br>` 把很多行粘在一起、纯文本超过 2000 字，也按同样的长度收成段。这样 Stratechery 里被收成 `<blockquote><dl><dt>` 的长粘贴不会再是几万字的一对。

普通段落（`p`、列表项、引用、表格单元、标题）里用 `<br>` 粘起来的很多行，也各成一段：非空行到三个就按行拆，两行的地址、诗句、署名不拆。写回仍留在原来那个 `<p>` 里，行间还是 `<br>`（`id="p-N"` 也留着），所以再切一次叶子数不变，#127 的 1:1 写回照样成立。22092 是这种：一个 `<p>` 里 75 个 `<br>`，以前整篇只出两对，现在一行一对。`dt` / `dd` 那条路不同，仍按上面的长度打包、写回时把整段换成若干 `<p>`。

入库时，模型返回的 `t` 必须和这批 `segments` 一样长，再按这些叶子段原位写回（外壳留着，只换那一段）。条数不一致时，先按原批重试一次；仍不一致就把这批对半拆开再译，直到一段对一段。只剩一段还对不齐，这一批失败，错位的 `t` 不入库。

写回后再用同一套叶子规则切译文，段数必须和原文一样，否则这篇不入库。模型返回的片段收成一个没有块级子节点的叶子：外层标签按原段留着，多出来的 `<p>` 并回这一段，没闭合的标签补上，避免后文的标题被吞进上一段。连续四条以上的 `dt` 里，单独成段的那一条写成 `<p>` 而不是更短的 `dt`，否则中文变短后会按字数重新打包，对照从这里开始错位。定义列表里用 `<br>` 拆开的段同样各自收成一段，不再按译文字数重拆。展示仍用这一套规则把原文和译文各切一次再按顺序并排。两边叶子数一致时按位置 1:1；只有两边都收掉同样多的连续重复段，对照才少掉那几段。段数对不齐的旧稿不另做硬配：同一套去重只在差距变小时拉齐，多出来的仍挂在末尾。

默认一批最多 12 段、24000 字符。超过的另起一批；单段比字符上限还长时单独成批，不从中间切开。整篇原文仍放在 `articleContext` 里（放得下的时候），供统一术语，不拿去凑 `t` 的条数。

有原文的段如果译成空白，这一批失败。译文只要非空就收下，不拿中英字符长度的比例做门槛。

一个译段内部，模型连吐的相同顶层块或相同 `<p>` 会收成一份。不跨段删：原文引用后又跟了一段相同正文时，两段译文都留着，中英叶子数保持一致，写回才过得了叶子数检查。展示时如果两边都因此变短且叶子数仍然一样，对照才少掉那几段，库里的 HTML 不改。已经只在中文侧删过这些段的旧稿不回写、不重译。

对照页永不因段数差退中文。展示层对原文和译文各跑一遍与单段相同的连续块去重（忽略 `id="p-N"`）。普通段落按 `<br>` 拆不拆，看拆开后两边的差额会不会变大：旧译文是整段译的，行数常和原文差一两行，拆开只会让对照从那行起整段错开，这时仍整段一对；新译文写回时行数一致，拆开就是一行一对。两边叶子段一样多时，只有两边都变短并且仍然一样长，才改用去重后的段，用来收掉引用后紧跟的同一段正文；只短一边会拉开长度，仍按未去重的等长序列并排，不单边丢掉。不一样多时，只有差距变小才改用去重后的段，用来把旧稿里的同段英中对上。对不齐的多余段挂在末尾：多出来的原文没有译文，多出来的译文没有英文。去重比的是块的 HTML，不是去掉标签后的纯文本，所以列表项和紧跟着的同文 `<p>` 不会并成一段。媒体仍只留在英文侧；译文和原文纯文本相同、或译文为空，这一段不显示中文。模型少段或多段仍然整批重试或拆批，不靠展示层去补条数。

已经按旧的粗分段译完、且译文里还留着内层标签的文章，部署后对照会按新的叶子段重排，不必先改库。库里的 `segments` 仍是当时的段数。要按新粒度重译，用 `recheck`：只抽「一个叶子仍包着很多段」或「记下的段数远少于现在能切出的叶子」的完整译文。远少是指块级叶子（不含普通段落里按 `<br>` 拆出来的行）至少多 15 段，并且达到原来的三倍，或多出 40 段。多条定义列表以前整段是一个叶子，会对上这条（21118 在 #119 之后大约记了 180 段，拆开后大约三百多段）。一条短定义列表仍是一段，不抽；一个 `<p>` 里几十行 `<br>` 也不只因为拆了行就抽，那几行本来就是一个整段一段译的。重译写回之后段数对齐，同一篇不会反复出现。

先看会重译哪些（不调模型）：

```bash
curl -s http://127.0.0.1:8088/api/v1/translate \
  -H 'content-type: application/json' \
  -d '{"recheck":true,"dry_run":true,"source":"stratechery","limit":200}'

# 全部英文来源（不传 source）
curl -s http://127.0.0.1:8088/api/v1/translate \
  -H 'content-type: application/json' \
  -d '{"recheck":true,"dry_run":true,"limit":200}'
```

确认篇数可以接受再开跑。每篇按新叶子重译，长文大约十几批 DeepSeek。不带 `recheck` 的 `loop` 只译还没有完整译文的，不会重跑这几篇。

```bash
curl -s http://127.0.0.1:8088/api/v1/translate \
  -H 'content-type: application/json' \
  -d '{"recheck":true,"source":"stratechery","limit":5,"loop":true,"sleep":60}'
```

容器里等价：

```bash
docker compose exec app research-translate --recheck --source stratechery --dry-run --limit 200
docker compose exec app research-translate --recheck --source stratechery --limit 5 --loop --sleep 60
```

点名几篇仍然可以：`research-translate --article-id 21118`。新导入的英文文章会自动排进译制（一次最多 20 篇）。大批回填超出上限的，用上面的接口续跑。设 `RESEARCH_INGEST_FOLLOW=0` 可以关掉导入后的自动识图和译制。

#121 之后已经重译过、但写回时把长 `dt` 收成更短结构的文章（21118、21133、21193 这一类），库里的 `segments` 等于那份错误译文的叶子数，`--recheck` 抽不到。部署后只改展示也不能把写丢的叶子找回来，必须点名整篇重译：

```bash
docker compose exec app research-translate --article-id 21118 --article-id 21133 --article-id 21193
```

重译完成前，对照仍会从差额处开始错位（同一段英文开头重复出现、标题配上「外部图片已省略」、文末中文空）。重译写回之后，原文和译文叶子数一致，再打开对照页即可，不必再 force 第二次。

## content_hash 与待译队列

`article_translations.content_hash` 必须等于 `articles.content_hash` 且 `complete`，这篇才算译完。`content_hash()` 把 `published_at` 固定按 Asia/Shanghai 打戳；若按 UTC `isoformat()` 重算会对不上，待译队列会把已译篇反复拿出来（`--limit 2` 时整队像卡住）。

时区漂移或改过日期后，跑：

```bash
PYTHONPATH=src python scripts/repair_content_hash_tz.py          # dry-run
PYTHONPATH=src python scripts/repair_content_hash_tz.py --apply
```

worker 同一次运行（CLI 的 `--loop` 或站内 API 的 `loop`）里失败过的 id 会先跳过，避免对齐失败把队列钉死。

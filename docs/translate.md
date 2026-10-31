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

## 译制：research-translate

在 Mac mini 上用容器里的命令跑。应用启动和部署都**不会**自动开译制，导入也不会，要自己跑：

```bash
docker compose exec app research-translate --source stratechery --limit 10   # 从待译队列取 10 篇，译完就退
docker compose exec app research-translate --article-id 123                  # 点名，可重复
docker compose exec app research-translate --source stratechery --limit 5 --loop --sleep 60   # 一直跑，队列空了睡 60 秒再扫
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--source` | 空 | 只译该来源 id；空表示所有来源，中文文章不进队列 |
| `--limit` | 5 | 每轮从待译队列取几篇 |
| `--loop` | 关 | 队列空后休眠再扫，直到 Ctrl-C |
| `--sleep` | 60 | `--loop` 空闲时休眠秒数，最少 5 |
| `--article-id` | 空 | 只译这几篇，可重复 |
| `--recheck`、`--dry-run` | 关 | 见下面重译旧分段的那一节 |

同一轮里失败过、没有可译正文或者没译全的篇先跳过；`--loop` 时队列只剩它们就先睡，睡醒再给一次机会，不会一直重试同一篇。没配 LLM 凭证时直接退出（退出码 2），不连库。

跑的时候自动上线会等它跑完再重建（`scripts/deploy.sh` 看 `docker compose top app` 里有没有 `research-translate`），这期间合并的改动要等这一批译完才上线。长时间的 `--loop` 放在 tmux 里跑，用完 Ctrl-C 停掉。

调用参数与 AIHOT 一致：`https://api.x.ai/v1/chat/completions`，Bearer 为 OAuth access token；过期或 403 stale 时强制刷新再试。长调用默认 25 分钟（`LLM_LONG_TIMEOUT_MS`）。

## Stratechery

全量归档已入库，`config/sources.yaml` 里 `stratechery` 是 `enabled: true`（#61）。盒子上再抓到 Passport 全文 / Daily≥5500 合格稿时：

1. `research-ingest-markdown --source stratechery --input …`
2. `docker compose exec app research-translate --source stratechery --limit 10`，一批译不完就加 `--loop`

游客半截 / guest teaser **不入库、不译**。

导出的引文、带段落的列表项和脚注都写了两遍：先是一行纯文字（链接、加粗都没了），紧跟着是里面每一段带格式的副本。单看原文时（文章页没有译文、点了「原文」，以及 `/api/v1/articles/{id}.md`）显示前把副本放回引文、列表项，纯文字那份不显示（`researchlib/nested_copies.py`）。库里的 `body_md` 不改，译制、检索、问库读的还是两遍。已有译文是照着两遍译的，中英对照照旧按位置配：过 80 字的重复对由对照页自己收，短的引文和脚注两遍都还在。以后译的也照两遍送进模型。

每日更新照邮件导出，开头的「Listen to this post:」「Good morning,」「On to the Update:」和结尾的播客提示、转发须知（「intended for a single recipient」「order multiple subscriptions for your team」）、「Thanks for being a subscriber, and have a great day!」几乎每篇都有。检索文字和段落块不收这几句，单看原文时也不显示（`researchlib/housekeeping.py`）；「Good morning,」和「On to the Update:」之间作者写的话（更正、回应读者、播客预告）留着。中英对照和只看中文照旧：已有译文里这几句也译了，对照按位置配，原文这边少几段就和译文错开。以后译的也照旧整篇送。

## 何时开跑翻译

| 状态 | 动作 |
|---|---|
| 译制管道已合入、LLM 凭证已配（DeepSeek key，或 Grok 已 login 且 `RESEARCH_LLM_PROVIDER=grok`） | 可对库内已有合格英文稿跑 `research-translate` |
| Stratechery 仍在抓 | **先不 ingest**；可用英文样例验对照页 |
| 抓完并 ingest 后 | 跑 `research-translate --source stratechery`；积压多就加 `--loop` |

## 环境变量

见 `.env.example`。默认 DeepSeek：`RESEARCH_LLM_PROVIDER=deepseek`、`DEEPSEEK_API_KEY`，可选 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_REASONING_EFFORT`、`DEEPSEEK_MAX_TOKENS`。Grok 路径：`XAI_OAUTH_TOKEN_PATH`、`XAI_OAUTH_FILE_UID`、`XAI_OAUTH_FILE_GID`、`XAI_BASE_URL`、`XAI_MAX_TOKENS`。两边共用 `LLM_LONG_TIMEOUT_MS`。

## 译制输入：和文章页同一条清洗

译制读的正文先过 `boilerplate.clean_article`，和文章页显示前那一遍是同一条：归档头、引流尾巴、二维码、星球导出脚手架都在送进模型之前去掉。`article_translations.content_hash` 仍按库里的 `body_md` 算，清洗规则变了不会把已有译文判成过期；重检用的原文段数（`list_coarse_translations`）也按清洗后的正文算。

不清洗就会把不该译的行也译出来。Stratechery 周报正文里有一行归档头 `- source: member-rss`，页面早就洗掉了，译制以前照原样送进模型，中文侧于是多一段「来源：member-rss」，英文侧没有，对照从第一对起整页错一行。22092 是这种：英 76 段、中 77 段，77 对里每一对都挪了一位。`- rss_link:` 同样是归档头，和 `- source:` 一起在送译和显示前去掉；以前没认的话，英文侧多一行链接、中文侧仍是「来源：member-rss」，从第一对起错位。

这条只管以后译的。改之前译好的篇目，译文里已经多出这一段，展示层不会自己去补，`--recheck` 也抽不到（22092 记的是 `segments = 2`、正文不到 8000 字），只能整篇重译。待重译的 Stratechery 译文这样点名（只查库，不改）：

```sql
SELECT a.id
FROM articles a JOIN article_translations t ON t.article_id = a.id
WHERE t.complete AND t.content_hash = a.content_hash
  AND t.body_html ILIKE '%member-rss%'
ORDER BY a.id;
```

2026-10-30 跑出来 38 篇，全是 stratechery。其中 14 篇的叶子数正好只多这一段（22092、22198、22240 那种「整页错一行」，中英从第一对起全体挪一位），其余是更早的粗分段写回，本来就该重译。重译要花 DeepSeek 的钱，按篇点名：

```bash
docker compose exec app research-translate --article-id 22092 --article-id 22198
```

重译写回后叶子和页面一致，不用再跑第二遍。

## 段对齐

叶子段和 AIHOT `segmentsOf` 同一条规则：标签在 `p`、`h2`–`h5`、`li`、`blockquote`、`figcaption`、`td`、`th`、`dt`、`dd`、`caption` 里，并且没有块级子节点，才是一段。有块级子节点就继续往下看。所以正文里的大段 `blockquote`（markdown 里连续的 `>`）会拆成里面的段落、标题和列表项；没有块级子节点的短引用仍是一整段 `blockquote`。

AIHOT 的 CONTAINER 不含 `dl`。#119 从 `editorial/translate.ts` 的 `segmentsOf` 移植时就是这样；这份 TypeScript 不在本仓库，公开检索也没有另一份可以对照，所以不把「AIHOT 会拆 `dl`」写成事实。研究站为了对照能读，在这条规则之外多走一步：叶子里的 `dl` 有多条 `dt` / `dd`，或纯文本达到 4000 字时，继续拆进这些子节点。只有一条、而且不长的定义列表仍是一整段 `blockquote`。术语表仍是一条 `dt` 配一条 `dd`。连续四条以上的短 `dt`（markdown 把粘贴的每一行收成一条）按句号收成段，大约一两句一段，不会变成几百个只有几个词的对照，也不从半句中间切开。单条 `dt` / `dd` 如果只是用 `<br>` 把很多行粘在一起、纯文本超过 2000 字，也按句收成段。这样 Stratechery 里被收成 `<blockquote><dl><dt>` 的长粘贴不会再是几万字的一对。

普通段落（`p`、列表项、引用、表格单元、标题）里用 `<br>` 粘起来的很多行，非空行到三个就先按行切开，再按句收成叶子：一行自己已经以句号结尾就单独成段，人名、链接文字这种半句并进后文，直到句末。两行的地址、诗句、署名不拆。不按 180 字把后面的短句再并进来，也不按定义列表那条 700 字的上限从半句切开——中文更短，同一批 `<br>` 会切出不同的组数；切在句末标点前时，剩下的「).」没有字母。这行不单独成叶，并进前一句（前面没有句子就并进后一句）。单独留下的话不会送去翻译，写回把其余行换成 `<p>` 之后再切会被丢掉，英文比中文多一片叶子。写回换成若干 `<p>`（`id="p-N"` 留在第一句上；原来是列表项或引用时外壳还在），译文里不再留 `<br>`。译文没有句号、或者比英文短，也不会被重新并段，再切一次叶子数不变。访谈稿里「super-agent / Ari Emanuel / . Emanuel is…」这种软换行因此是一整句，而不是每个链接各一对。`dt` / `dd` 也不再按 700 字从半句切开：句末才收，下一条已经很长时，前面没写完的「( example.com」留在同一句里，不单独成叶。

没有够三行 `<br>`、但纯文本超过 900 字并且里面有多句的叶子，同样按句切开再写回成若干 `<p>`。21118 那种两千字的列表项、22195 引用里用一个 `<br>` 粘起来的两千字，因此不再是一对。网址、小数和 `Inc.` 这类缩写里的句号不当成句末。一整句再长也不从半句撕开。写回之后再切，叶子数不变。

入库时，模型返回的 `t` 必须和这批 `segments` 一样长，再按这些叶子段原位写回（外壳留着，只换那一段）。条数不一致时，先按原批重试一次；仍不一致就把这批对半拆开再译，直到一段对一段。只剩一段还对不齐，这一批失败，错位的 `t` 不入库。

写回后再用同一套叶子规则切译文，段数必须和原文一样，否则这篇不入库。模型返回的片段收成一个没有块级子节点的叶子：外层标签按原段留着，多出来的 `<p>` 并回这一段，没闭合的标签补上，避免后文的标题被吞进上一段。连续四条以上的 `dt` 里，单独成段的那一条写成 `<p>` 而不是更短的 `dt`，否则中文变短后会按字数重新打包，对照从这里开始错位。定义列表里用 `<br>` 拆开的段同样各自收成一段，不再按译文字数重拆。展示仍用这一套规则把原文和译文各切一次再按顺序并排。两边叶子数一致时按位置 1:1；只有两边都收掉同样多的连续重复段，对照才少掉那几段。段数对不齐的旧稿不另做硬配：同一套去重只在差距变小时拉齐，多出来的仍挂在末尾。

默认一批最多 12 段、24000 字符。超过的另起一批；单段比字符上限还长时单独成批，不从中间切开。整篇原文仍放在 `articleContext` 里（放得下的时候），供统一术语，不拿去凑 `t` 的条数。

有原文的段如果译成空白，这一批失败。译文只要非空就收下，不拿中英字符长度的比例做门槛。

一个译段内部，模型连吐的相同顶层块或相同 `<p>` 会收成一份。原文里同一段正文出现两次（引用后再贴一遍，或列表项后再跟一段同文）时，后一次沿用第一次的译文，写回仍然一段对一段，不删槽位。只在中文侧少了这段的旧稿不回写、不重译。

对照页永不因段数差退中文。展示层对原文和译文各跑一遍与单段相同的连续块去重（忽略 `id="p-N"`）。普通段落按 `<br>` 拆不拆，看拆开后两边的差额会不会变大：旧译文是整段译的，行数常和原文差一两行，拆开只会让对照从那行起整段错开，这时仍整段一对；新译文写回时叶子数一致，拆开就是按句成对。两边叶子段一样多时按位置收，不把两边各自收完再比条数。后一对的英文够长（可见文字大约 80 字以上），和前面某一对相同、只是它的开头、或几乎一样（长度差在一成以内、相似度 0.96 以上），并且这一对的中文也是前面那一对中文的同一段，两对一起收掉。中文译文通常更短，不到 80 字也算同一段；后一对没有译文也收掉。英文重复但中文是另一句的不收，后面的段落也不会因此错位。隔了几段又贴出来的同一句、同一组句子都走这一条，不要求紧挨着。列表项和同文的 `<p>` 也是。只共享开头、后文不同的两段都留着。英文短于大约 80 字的不按这条收。够长的一句若整句包含在另一对更长的英文里，收掉短的那一对，长的留下，后面多出来的句子还在。这一比较只压空白、忽略大小写，不去掉标点，两条来源注释只是括号不同时都留着。中文不必逐字嵌在长段译文里，分开译的两遍措辞不同也收短句。标签边界按空格计，所以正好 80 字的一句不会因为中间套了 `<strong>`、句号贴在标签上就少算一格。较短的一句若有大约四分之三是较长那句的开头，两句长度差在一成以内，中文又共享一长串（大约 12 个字，措辞可以不同），收掉短的，长的留下。只共享大约三分之二开头、后文明显岔开的两份提示仍都留着。英文整句相同、中文却是另一句的，仍然两对都留。以前两边各自按 80 字去收，中文更短就少收几对，条数对不齐就整页退回，隔开的长英文会留下两三次。不一样多时，两边仍各收各的长叶子，只有差距变小才改用那一份，只短一边不硬配。对不齐的多余段挂在末尾：多出来的原文没有译文，多出来的译文没有英文。媒体仍只留在英文侧；译文和原文纯文本相同、或译文为空，这一段不显示中文。模型少段或多段仍然整批重试或拆批，不靠展示层去补条数。

旧译文若还是一整段对着现在按句切开的原文，拆开会让差额变大，对照仍用整段那一份，直到点名重译。重译写回后叶子数一致，才按句并排，重复段也收成一份。

已经按旧的粗分段译完、且译文里还留着内层标签的文章，部署后对照会按新的叶子段重排，不必先改库。库里的 `segments` 仍是当时的段数。要按新粒度重译，用 `recheck`：只抽「一个叶子仍包着很多段」或「记下的段数远少于现在能切出的叶子」的完整译文。远少是指块级叶子（不含普通段落里按 `<br>` 拆出来的行）至少多 15 段，并且达到原来的三倍，或多出 40 段。多条定义列表以前整段是一个叶子，会对上这条（21118 在 #119 之后大约记了 180 段，拆开后大约三百多段）。一条短定义列表仍是一段，不抽；一个 `<p>` 里几十行 `<br>` 也不只因为拆了行就抽，那几行本来就是一个整段一段译的。重译写回之后段数对齐，同一篇不会反复出现。

先看会重译哪些（不调模型）：

```bash
docker compose exec app research-translate --recheck --dry-run --source stratechery --limit 200
docker compose exec app research-translate --recheck --dry-run --limit 200   # 全部英文来源
```

确认篇数可以接受再开跑。每篇按新叶子重译，长文大约十几批 DeepSeek。不带 `--recheck` 的 `--loop` 只译还没有完整译文的，不会重跑这几篇。

```bash
docker compose exec app research-translate --recheck --source stratechery --limit 5 --loop --sleep 60
```

点名几篇：`research-translate --article-id 21118`。

#121 之后已经重译过、但写回时把长 `dt` 收成更短结构的文章（21118、21133、21193 这一类），库里的 `segments` 等于那份错误译文的叶子数，`--recheck` 抽不到。部署后只改展示也不能把写丢的叶子找回来，必须点名整篇重译：

```bash
docker compose exec app research-translate --article-id 21118 --article-id 21133 --article-id 21193
```

重译完成前，对照仍会从差额处开始错位（同一段英文开头重复出现、标题配上「外部图片已省略」、文末中文空）。重译写回之后，原文和译文叶子数一致，再打开对照页即可，不必再 force 第二次。

超长多句叶子（21118 的 Wearables / Services 一类两千字列表项加同文段落，22195 的 iPhone Duo 引用大约两千英文字）按句切开之后，同一段英文还会隔开再出现两三次：译文常常不到 80 字，两边各自去重对不齐条数，展示就退回未去重的序列。叶子数已经对齐时按位置收：中文更短也算同一段；短句整句包含在更长的一段里时收掉短句、留下长段。这些重复不必再译。页面上不应再有大约两千字的一对，也不应再有隔开的同一段长英文，或短句和把它包含进去的长段各出现一次。21130 的叶子数已经对齐，近重复段（例如 Meta 开头、Judge Alsup）走同一条。旧稿若还是整段一对，拆开会让差额变大，对照仍用整段那一份，直到点名重译：

```bash
docker compose exec app research-translate --article-id 21118 --article-id 22195
```

差额变大时不会为了拆句把后面的段落错位。

## 对照质量检查：research-bilingual-qa

`research-bilingual-qa` 把人工抽验对照页时看的几条硬指标做成可重复的命令。它按文章页的顺序重算对照：原文同一遍清洗、模板尾巴、md 链接改写和图片文字，译文同一遍白名单，再走 `pair.bilingual_pairs` 和展示用的同一份切叶。只判断，不改库、不重译、不跑 OCR。

| 指标 | 代码里的名字 | 判定 |
| --- | --- | --- |
| 巨型 pair | `giant-pair` | 单对英文可见文字 ≥ 2000 字。按句切开之后不该再有 |
| 近重复组 | `near-duplicate` | 页面上仍是重复、整句包含或近前缀堆的长英文。展示层只在两边叶子一样多时按位置收，条数对不齐的旧译文会留下 |
| long-empty-ZH | `long-empty-zh` | 对上的那一对英文 ≥ 80 字，中文为空。短行、纯标点可以缺 |
| 归档头 | `archive-header` | 正文里还留着 `member-rss`、`rss_link`、`- source:` 这类行。`来源：` 单独出现可能是正文里的正常说法，不单独认 |
| 叶子/对数 | `leaf-gap` | 展示硬排的两边叶子差。差 40 段以上按失败算，差一两段只告警 |

任一项失败就是 `FAIL`，只有告警是 `WARN`，都没有是 `PASS`。报告里同时给细分叶子数（`leaves=英文/中文`）、展示实际用的切法（`display=英文/中文`）和最长一对的英文字符数（`maxEN`）：细分叶子差很大、展示却只差几段，说明这篇还是旧的粗分段译文，展示退回了整段一对。

```bash
docker compose exec app research-bilingual-qa --article-id 21118
docker compose exec app research-bilingual-qa --article-id 21118 --article-id 22195 --article-id 22092
docker compose exec app research-bilingual-qa --source stratechery --limit 20
docker compose exec app research-bilingual-qa --limit 50 --json --report /data/status/bilingual-qa.json
docker compose exec app research-bilingual-qa --article-id 21118 --llm --llm-sample 1
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--article-id` | 空 | 点名，可重复 |
| `--source`、`--limit` | 空、20 | 不点名时按译文更新时间从新到旧取最近几篇 |
| `--json` | 关 | 输出一个 JSON 对象 |
| `--report` | 空 | 另写一份 JSON 文件；`/data/status/` 在宿主机上是 `~/research/data/status/` |
| `--strict` | 关 | 有警告也返回 1 |
| `--llm`、`--llm-sample` | 关、3 | 对没有失败项的篇再跑 LLM 抽验，默认最多 3 篇 |

退出码：0 通过（或有警告但没加 `--strict`），1 有失败项（硬指标，或 `--llm` 时模型给 `fail`；加 `--strict` 时警告也算），2 参数、库或 LLM 的问题。只读：连库走 `db.connect`，不跑迁移、不写表；`--report` 只写你给的那个文件。

### LLM 抽验

`--llm` 用站内默认模型（DeepSeek `deepseek-flash`、thinking、`reasoning_effort=max`，和译制、问库共用 `researchlib/deepseek.py` 的常量），判断抽样段落是否成对。每篇最多 12 对：先取硬指标点名的那几对，再按整篇的头、中、尾补；每段截到 1500 字并标 `truncated`，提示词里写明截断不算漏译、错位。提示词在 `researchlib/translate/qa.py` 的 `LLM_SYSTEM`，只判断 `misaligned` / `missing-zh` / `duplicate` / `other`，不改写译文。

模型返回 `verdict` / `score` / `issues` / `summary`，打印在报告里，`--json` 和 `--report` 也带上。`--llm` 默认跳过硬指标已经失败的篇，钱先花在看不出问题的篇上。模型给 `fail` 时整条命令退出 1；给 `warn` 只在 `--strict` 时退出 1。凭证不对、接口错、JSON 收不成结论都退出 2，硬指标的结果照常打印。整个命令不写库。

一次抽验十几对、几千字，跑 3 篇费用很小；但不要在启动或部署时自动跑，也不要接进夜间任务全库扫。旧译文失败多，先按篇点名。

### 锚点（2026-10-31 在正式库上跑出来的）

| 文章 | pairs | 巨型 | 近重复 | long-empty | 叶子差 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| 21118 | 489 | 0 | 0 | 0 | 0 | PASS |
| 22195 | 60 | 0 | 0 | 0 | 0 | PASS |
| 22092 | 17 | 0 | 0 | 0 | 0 | PASS |
| 22209 | 340 | 0 | 0 | 0 | 2 | WARN |
| 21130 | 52 | 2 | 8 | 0 | 3（细分 107/66） | FAIL |

21118 的 489 对和人工抽验一致；页面上的 `bilingual-pair` 也是 489 个。22195、22092 同样对得上。

22209 硬指标只告警：文末两段英文没有中文。LLM 抽验抽到正文 112–114、225–227 两处中段错位，人工核对属实（英文讲 TKO 晨会和 AI 设计，中文对到了别的问答）；要修只能点名重译，本 PR 不做。

21130 还是旧的粗分段译文：细分 107/66，展示退回 49/52 那一切法，页面上仍有两对约 3900 字的英文和 8 处重复。它要重译才会变，本 PR 不改库。

旧译文大多会 `WARN` 或 `FAIL`，这不代表新命令坏了：它量的是页面现状。给新译的文章跑、或按篇点名跑，才有门禁意义。译完一批后可以 `--source <id> --limit <篇数>` 看这一批的数字。

## content_hash 与待译队列

`article_translations.content_hash` 必须等于 `articles.content_hash` 且 `complete`，这篇才算译完。`content_hash()` 把 `published_at` 固定按 Asia/Shanghai 打戳；若按 UTC `isoformat()` 重算会对不上，待译队列会把已译篇反复拿出来（`--limit 2` 时整队像卡住）。

时区漂移或改过日期后，跑：

```bash
PYTHONPATH=src python scripts/repair_content_hash_tz.py          # dry-run
PYTHONPATH=src python scripts/repair_content_hash_tz.py --apply
```

worker 同一次运行（CLI 的 `--loop` 或站内 API 的 `loop`）里失败过的 id 会先跳过，避免对齐失败把队列钉死。

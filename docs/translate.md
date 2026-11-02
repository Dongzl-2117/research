# 站内译制（中英对照）

英文原文入库后，由 **investment / research 站内** 调 LLM 翻译（默认 DeepSeek，可切 Grok OAuth），排版对齐 AIHOT 文章页：

- 默认 **对照**：每段英文在上（小一号、次要色 `--ink-4`），中文在下
- Tab：**对照 / 中文 / 原文**（`/articles/{id}`、`/zh`、`/original`）
- 媒体与图片只在英文侧出现一次
- 默认 `deepseek-flash`：thinking 打开、`reasoning_effort=max`、上下文 1M token、输出至少 393216，不发 temperature。`RESEARCH_LLM_PROVIDER=grok` 时改用 `grok-4.7`、`reasoning_effort=xhigh`、上下文 50 万、输出至少 128000，不用 fast。常量在 `researchlib/deepseek.py` / `researchlib/grok.py`，问库、摘要、观点、论点、样卡走同一套

不要站外译完再贴进库。译文表是 `article_translations`。原始 `content_hash` 保留待译队列兼容；新写入另外保存 `provenance`，核对实际清洗输入、处理版本和逐段身份。结构检查通过不代表已经审核译义。

标题另译一次（存 `title_zh`）。标题失败时新稿记 `partial`；已有完整译文不会被这次不完整结果覆盖。译文写入后切成中文段落块进检索（`article_chunks` 里 `lang = 'zh'`，带对照页锚点），用中文能搜到、问到英文文章，见 [retrieval.md](retrieval.md)。启动时会给有译文但还没有译文块的文章补切。同一篇的重复不译。

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

导出的引文、带段落的列表项和脚注可能写两遍。单看原文时仍由 `nested_copies.py` 保留格式和资料链接，库里的 `body_md` 不改。译制保留每次出现的身份和顺序，模型返回后不跨段复制译文。对照页只折叠两侧文字、资源和有效结构都精确相同的整对；不同中文措辞、不同数字或资料链接都保留。旧译文数量不齐时明确分别保留完整原文和译文，不通过各自去重凑齐条数。

每日更新照邮件导出，开头的「Listen to this post:」「Good morning,」「On to the Update:」和结尾的播客提示、转发须知（「intended for a single recipient」「order multiple subscriptions for your team」）、「Thanks for being a subscriber, and have a great day!」几乎每篇都有。检索文字和段落块不收这几句，单看原文时也不显示（`researchlib/housekeeping.py`）；「Good morning,」和「On to the Update:」之间作者写的话（更正、回应读者、播客预告）留着。邮件导出的标题行下面常常不空行就跟着「**Listen to this post:**」，标题行单独算一段；2026 年网页导出（`- source: member-rss`）整篇一行一段、中间没有空行，开头的几句按行认，结尾从最后一行往前找最长的一截套话。中英对照和只看中文照旧：已有译文里这几句也译了，对照按位置配，原文这边少几段就和译文错开。以后译的也照旧整篇送。

## 何时开跑翻译

| 状态 | 动作 |
|---|---|
| 译制管道已合入、LLM 凭证已配（DeepSeek key，或 Grok 已 login 且 `RESEARCH_LLM_PROVIDER=grok`） | 可对库内已有合格英文稿跑 `research-translate` |
| Stratechery 仍在抓 | **先不 ingest**；可用英文样例验对照页 |
| 抓完并 ingest 后 | 跑 `research-translate --source stratechery`；积压多就加 `--loop` |

## 环境变量

见 `.env.example`。默认 DeepSeek：`RESEARCH_LLM_PROVIDER=deepseek`、`DEEPSEEK_API_KEY`，可选 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_REASONING_EFFORT`、`DEEPSEEK_MAX_TOKENS`。Grok 路径：`XAI_OAUTH_TOKEN_PATH`、`XAI_OAUTH_FILE_UID`、`XAI_OAUTH_FILE_GID`、`XAI_BASE_URL`、`XAI_MAX_TOKENS`。两边共用 `LLM_LONG_TIMEOUT_MS`。

## 输入、身份和写入校验

译制与文章页共用 `boilerplate.clean_article`，归档头、引流尾巴和导出脚手架在送译前清除。随后渲染、抽叶子、保护图片/代码/链接，按源叶子的位置写回。列表、表格、标题和非可译内容仍在原结构中。

- 每批最多 12 段、24000 字符。单段超限时独立成批，不从半句切开。
- 请求含 `segments` 和同序的 `segmentIds`。响应为 `{"ids": [...], "t": [...]}`；`ids` 必须与请求完整一致，不接受缺少、重复、未知、换序或非文本译段。
- 数量不一致仍先重试、再拆批；拆批沿用原段身份。数字、链接、图片和代码按段核对，检查出现次数及资源归属，不拿全篇总数代替。
- `unshield` 失败、图片/代码占位符或链接身份丢失、重复、未知时拒绝该批。恢复和事实校验包含在有界重试内，第二次失败即抛错，不把未恢复响应当作译文保存。
- 原文相似不等于同一事实；不再用相似度、共同前缀或可见链接文字覆盖另一段的正确译文。即使原文完全相同，也保留各次实际返回的译文。
- 写回后再次切段并逐段校验。失败时不更新译文、完成状态或检索块；命令继续处理其他篇，但单次运行有失败或 `partial` 时退出 1。

数字采用保守的字面校验。提示词要求保留阿拉伯数字，不自动接受金额换单位、改用中文数词等变化；这可能拒绝语义合理的改写，避免把事实变化当成正确译文。模型仍可能在数字和资源相同的段之间写错译义，不能仅凭身份列表证明语义。

## 对应记录与旧稿兼容

迁移 `028_translation_provenance.sql` 只增加可空 JSONB 列，不回填正文，不改旧行的 `complete`、hash、时间或队列资格。启动没有新增重算、历史写回或付费任务。

新写入的 `provenance` 包含：

- 清洗、渲染、分段和响应协议版本（`integrity.PROCESSING_VERSION`）；这些规则的语义改变时须递增对应版本。
- 实际渲染输入指纹与完整译文指纹；只有读取时的 `p-N` 锚点不参与指纹。
- 按文档顺序排列的源段身份、原下标、源段指纹、同文出现次数、数字、资源以及对应译段指纹。

页面和 QA 在展示整理之前重新核验同一份规范输入。即使原始 `body_md` 没变，清洗结果从 3 段变成 2 段也能报 `canonical-input-changed`。记录或译文不一致时显示完整两侧并告警，不继续假定旧顺序有效。

没有记录的旧稿标 `legacy-unverified`，保留现有内容与完成状态。数量相等且没有明显事实冲突的旧稿仍可逐段展示，但页面会注明尚未核验对应关系，QA 至少为 `WARN`。处理版本和输入指纹的变化不自动加入待译队列。原始 hash 变化的原有队列规则继续保留。

## 展示完整性

保留 #212 的完整区块回退和 #215 的「未去重细对 → 投影检查 → 展示折叠」顺序。不再让近似去重删除另一种译法，也不通过两边独立删段证明对应。

投影核对文字、标点、出现次数与顺序，以及图片、链接和有效列表/表格结构。只允许无属性列表外壳的有限兼容缺口；纯数字单元格、独立图片、代码和标题不能借“叶子本来没抽到”而消失。共享图片可在原文侧显示一次，独有译文资源保留。

两侧精确重复的整对可折叠，投影单独记录这一许可；检测器误把独有段落报为重复时，独立保留检查会拦住。前缀相同、相似度高、英文包含另一句、中文共享开头，均不能单独授权删去另一份内容。

源与译的数量或数字/资源归属冲突、对应记录过期，且不能无损逐段展示时，分别显示完整两侧并提示。这是保留内容的回退，不是已修复逐段对应。整块或单句不能安全细分时仍可能超过 2000 字，QA 会报 `giant-pair`，不能用较漂亮的细对统计掩盖实际页面。

## 只读质检与旧稿分类

`research-bilingual-qa` 使用 `db.connect`，数据库连接设为只读，不跑 `db.prepare`、迁移、OCR、历史写回或模型。只有显式 `--llm` 才会调用模型。

```bash
# 升级代码后，先只读点名或小范围分类；本次开发没有在正式库执行这些命令。
docker compose exec app research-bilingual-qa --article-id 123 --classify --json
docker compose exec app research-bilingual-qa --source stratechery --limit 20 --classify --json
```

`--classify` 不能与 `--llm` 混用，输出的 `legacyPlan` 是后续审查建议，不会写回身份、完成状态或发起重译。

| 分类 | 含义与后续 |
| --- | --- |
| `recorded` | 当前输入和有序记录一致；结构已校验，语义仍未审核 |
| `lossless-display-only` | 当前展示没有已知内容损失；旧稿仍未获得语义证据，不回填成已核验 |
| `unmapped-review` | 无法可靠逐段对应；先核对旧稿，区分可无损重建的格式问题与真正缺译 |
| `retranslation-review` | 事实归属、受保护内容或记录存在冲突；须核对原稿，缺失译义才进入另行授权的重译清单 |

分类不能单独证明历史译义缺失。只有源段身份与译段证据一致、独有措辞及资料完整时，才可提出无损重建补丁；仅靠段数相等、去重后相等或文字相似不能补写身份。真正缺译/错译的篇目单独估算费用并请求授权，保留旧译文快照，逐篇验收；本次不执行。

QA 分开报告以下问题：

| 项 | 判定 |
| --- | --- |
| `mapping-unverified` | 无旧稿对应证据，告警 |
| `mapping-invalid` / `mapping-facts` | 记录过期，或数字、链接、图片、代码属于其他段，失败 |
| `display-fallback-separate` | 实际页面整篇分开展示，失败 |
| `giant-pair` | 实际页面一对英文 ≥ 2000 字，失败 |
| `near-duplicate` | 兼容名称；只统计仍可安全折叠的精确重复整对 |
| `long-empty-zh` | 实际页面的长英文没有中文；原段只有中文时不误报 |
| `leaf-gap` | 原始细段差与展示切法差都检查，不用较小差值掩盖原始差额 |
| `display-content-loss` / `translation-content-loss` | 内容出现次数、顺序或正文资源缺失，失败 |
| `translation-material-loss` / `protected-content-loss` | 译文资料或数字/图片/代码遗漏，失败 |

JSON 保留 `fineGap`、`displayGap`、`displayNotice`，增加 `mappingStatus` 和 `semanticReview: not-reviewed`。结构 `PASS` 不等于语义通过。退出码 0 表示无失败（默认允许告警），`--strict` 下告警也返回 1；参数或模型问题返回 2。`--report` 只写指定报告文件。

## 历史数据与验收边界

没有执行生产资料修复或付费翻译。历史金额被覆盖、漏译、等段数错配或资料丢失，升级代码不能凭空恢复；需要按上述只读分类核对。旧版报告里 #215 的回退数量、叶子数及 `PASS` 只是当时结构观测，不能充当独立语义证据，也不作为本次旧库验收结果。

本次修复、已存在能力、验证及剩余边界见 [交接记录](bilingual-integrity-2026-11-02.md)。

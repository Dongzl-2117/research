# 文章展示、订阅地址和 OCR 引用交接

基于 2026-11-01 的 main `9b678ca`（含 #211）。只实现、测试和提交草稿 PR；没有访问生产网站或资料库，没有调用翻译、识图、摘要模型，没有合并或部署。

## 本次修复与主线核对

| 问题 | 现状和本次处理 | 验收边界 |
| --- | --- | --- |
| 双语页漏掉纯数字、图片、代码、一级/六级标题，表格/列表失去外壳 | 原因仍存在：可译叶子不是完整文档。普通段仍细分；投影漏了独有文字、数字、资料链接、图片或结构时改用完整区块。 | 合成单测和真实路由覆盖原文、对照、中文；未读实际文章。 |
| 旧译文缺少中段后，后续中英文按位置错配 | 叶子数无法对齐时分别显示完整原文和译文，并提示读者，不再把差额一律挂到末尾。 | 条数一样仍可能语义错位；没有可验证的逐段持久映射，不能宣称语义对齐。 |
| 去重吞掉附加资料 | 同文副本带独有链接时必须保留；不为减少重复删掉该链接。相同文字的不同译文链接同样保留。 | 保留信息优先，个别近重复段可能重新出现。 |
| 结构 QA 两边同漏内容仍通过 | 增加独立于可译叶子的图片、数字、代码检查，以及实际展示投影检查；JSON 新增 `preservation` 和 `displayNotice`。 | 原有巨型段、去重、叶子差仍是潜在配对指标，不能当语义验收。词集合检查无法发现所有同词错序。 |
| 数百字误作 H1 | 正文标题超过 180 字时恢复为完整段落，去掉误标题内联样式；短 H1 显示为 H2，文章页主标题保留 H1。原文不回写。 | 180 字是确定性的展示规则，极长的真实标题也会按段落显示。 |
| 财务宽表挤断数字 | 单元格有最小可读宽度，取消继承的任意位置折行；表格保留横向滚动并能获得键盘焦点。 | 1440/390 像素本地 Chromium 均验证，15 列数据单行，页面未撑宽。 |
| 长文没有章节导航 | 超过 12,000 字符且至少 3 个标题时，浏览器生成可折叠目录，沿用已有标题 id，并避免中英标题重复。 | 无 JavaScript 或没有章节的长文不生成目录。 |
| 飞书内嵌表格资料缺失 | 已知「内嵌表格未取到」占位明确提示原导出缺表，需补资料。 | 没有恢复缺失表格，仍需重新导出。 |
| 个人订阅链接仍从旧块等路径输出 | #200 已过滤新正文归档行。本次补模板、JSON、Markdown、MCP 输出边界；兼容旧 `rsslink` 形式和已知带凭据的 Stratechery 地址。先遮地址再截断/高亮，避免只剩地址尾巴。 | 不改库，不作废凭据；只识别明确元数据和已知地址形状，不能保证任意截残的未知凭据片段。 |
| OCR 重切后公司观点跳到邻图 | 重切时用完整原句重新定位新块，同句优先原图，找不到就清定位。读取已有观点时也校验当前块确实含原句，不让旧号直接复用。 | 原句和观点保留；未执行历史重建。历史图身份未单独持久化，同句多图的历史歧义无法全部恢复。 |
| 有意义的图片说明以扩展名结尾被删 | #192 的通用扩展名规则收窄到默认截图名和哈希。`腾讯收入结构.png` 等说明保留。 | 已入库的检索文字没有自动恢复，需另行确认历史清洗。 |
| 翻译分段与识图噪声 | #135/#138、后续重复段规则和 #147/#209 等已在主线，不重做这些实现。#208 已在取标题前过滤读者评论。#203 的说话人身份策略沿用，不另改隐私规则。 | 未验收真实 OCR 准确率，未改其他作者的分支或历史提交。 |

## 上线影响

没有新增迁移、依赖或启动任务，也没有新的全库重写。展示和输出过滤在读取时生效。`replace_ocr_chunks` 的现有调用会同步维护该文章的观点定位；这不会自行发起 OCR、重切或付费任务。已错号的观点在读取时失去无证据的定位，但原句不消失。

主线已有的迁移、后台维护和夜间任务并未重写，不应把本 PR 理解成停用了它们。保持草稿；不要为了触发 CI 转成待审。仓库当前 CI 对草稿返回 skipped，skipped 不等于通过。

## #212 复核补修

初始提交 `db0f1403` 的区块回退仍有一次文字去重，只把原文链接/图片计入身份；两份英文和中文文字一样、中文链接分别是 `/a`、`/b` 时，后一条中文链接仍会被删掉，中文图片也有同类遗漏。

补修移除回退路径中的二次文本去重，保留每个原始区块；初始常规去重仅在内容投影完整时继续使用。区块处理全部结束后再次检查最终投影，发现遗漏则回到分别完整展示，并给出提示。展示与 QA 共用最终投影检查；共享图片在原文侧显示一次就算已保留，不要求译文再复制一张。

新增三个回归：同文译文的不同链接、不同图片，以及后续区块整理误删链接后的最终保护。三个用例在 `db0f1403` 上全部失败，修复后通过。完整区块回退中可能多留重复内容，这是内容保留优先的取舍。

## 本地复现

依赖照仓库锁文件安装。测试库必须是专门创建的合成库，带 zhparser 和 pgvector；下面的端口是本次独立实例，不应指向 mini 正式库。

```bash
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps -e .
pip check
ruff check .
DATABASE_URL=postgresql://research:research@127.0.0.1:55436/research_test pytest tests/unit tests/integration
python tools/retrieval_eval/gen_corpus.py --articles 1500 --url postgresql://research:research@127.0.0.1:55436/research_eval
EVAL_URL=postgresql://research:research@127.0.0.1:55436/research_eval python tools/retrieval_eval/evaluate.py --check tools/retrieval_eval/baseline.json
```

生成器只认 `--url`，不会读 `DATABASE_URL` 或 `EVAL_URL`；必须显式指定。首次运行曾漏传 `--url`，生成到现有本地 `research_eval`，不是正式库；最终证据来自 55436 独立实例的重新生成和评测。

关键回归可单独跑：

```bash
pytest tests/unit/test_article_preservation.py tests/unit/test_output_safety.py
DATABASE_URL=postgresql://research:research@127.0.0.1:55436/research_test pytest tests/integration/test_article_preservation_page.py tests/integration/test_feed_address_api.py tests/integration/test_ocr_quote_references.py
```

已用同一合成测试在干净 main 运行：新正文过滤用例通过；旧片段过滤、双语保留、OCR 引用三项失败，修复后通过。不是只增加无失败能力的测试。

浏览器验收可在 mini 或其他机器重跑。`playwright-core` 和 Chromium 只是可选验收工具，没有加入应用依赖。

```bash
npm install --prefix /tmp/research-ui-check playwright-core
NODE_PATH=/tmp/research-ui-check/node_modules ARTICLE_PYTHON=python ARTICLE_CHROMIUM=/path/to/chromium node tools/article_layout_smoke.cjs
```

它只生成临时合成 HTML，套用真实模板、CSS、脚本；不启动主应用、不连库、不读凭据，不允许浏览器请求外网。检查桌面/手机宽表、完整数字、图片、代码、缺表说明、目录跳转、错位提示和 JavaScript 错误，并在打印的临时目录留下截图。路由保真另由集成测试覆盖。

最终验证：760 项单元测试、150 项集成测试，共 910 项通过，零失败、零跳过；ruff、依赖一致性和差异空白检查通过。1,500 篇 / 10,463 块合成语料通过原检索基线。浏览器两种宽度均通过，数字单行、页面未横向撑开、目录跳转有效、零 JavaScript 错误。数据库为 PostgreSQL 16.15 / zhparser 2.4 / pgvector 0.6.0；没有验证 halfvec。

汇总证据和代码文件校验值见 [evidence/article-preservation-2026-11-01.json](evidence/article-preservation-2026-11-01.json)。

## 未运行的历史验收

没有独立复核 #183 的 417 篇统计，也没有改写旧译文。需要 mini 上获准的只读结构质检和人工中英文抽验；结构通过不证明语义正确。`research-bilingual-qa` 不带 `--llm` 时不调模型，下面只是后续交接示例，本次未运行：

```bash
PGOPTIONS='-c default_transaction_read_only=on' research-bilingual-qa --source stratechery --limit 2000 --json --report /tmp/bilingual-qa-private.json
```

报告可能含私有标题、摘录，留在私有目录，不上传 GitHub。历史检索文字、OCR 块、旧回答快照的进一步检查和修复另行安排；只更新代码不等于它们已重建。尤其 `research-rebuild-chunks --ocr --dry-run` 现有入口会先调用 `db.prepare`，不能把这个参数当成数据库只读保证。本次没有运行它，也没有安排自动执行。

个人订阅地址是否仍有效、是否需要由账户持有人作废，不属于代码过滤能解决的事项；没有查看、访问或保存任何真实访问地址。

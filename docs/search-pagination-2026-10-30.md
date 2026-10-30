# 文章搜索完整分页：2026-10-30 交接

## 修复对象和可见结果

初次版本基于 main `d88b6022c5e0a8191365ddb0bc18a6e2d22bf469`，后续同步及重新验证见下一节。main 的段落召回有数量上限，但分页曾只对这些段落覆盖的文章切片，计数却按全库文章索引计算。独立合成回归在旧代码复现：2,407 篇全部命中，`available` 只有 2,300；长文有 2,301 个匹配块时也会挤掉其他文章。

现在先列有限候选里的相关结果，再用与计数相同的 SQL 条件补齐其余文章，最后列部分匹配。补齐区按发布时间、文章号降序；全文词可以散在不同段落。相关度不是全库穷举计算，完整可达与排序窗口的具体语义见 [检索文档](retrieval.md#文章检索的完整分页)。本次没有公司专属分支、提高召回上限、引入新模型或全库取文。

补齐页每篇最多加载 8 个片段候选，再选两段；有正文命中时不使用无关开头，也不会让无关译文挤掉命中的原文。没有块的历史文章仍可打开，片段留空。全部命中、语义提升按文章号数并集，避免原来的 `max(索引计数, 候选计数)` 低估总数。

文章索引并集同时由 `UNION ALL` 改为 `UNION`。首次验证的 1,500 篇合成库中，旧查询把可见范围估成 1 篇，实际 1,500 篇，选择嵌套循环半连接，反复扫描全文向量；去重并集只聚合文章号，执行计划改为一次聚合与哈希连接。独立查看的标题候选 SQL 新计划执行约 12 ms。这是旧主线条件下的观察；主线简化可见性规则后应看新测量，不沿用旧耗时。

## 同步主线后的重新验证（当前）

在原 PR #142 分支上合入 main `87e64940815d2c671e34c41d2300dded1027f218`（含 #138、#144、#147），保留旧提交 `423c568` 的历史，没有变基或强推。程序与测试的验证提交为 `0fe4db3eb2419e568aff0495c4c546096cf56064`；后续只补文档和证据。本节与 [新证据 JSON](evidence/search-pagination-compat-2026-10-30.json) 是当前结果，下面的 745 项和旧耗时仅作历史记录。复核时 #148 仍未合并，不在此快照中。

[维护者指出](https://github.com/Dongzl-2117/research/pull/142#issuecomment-6515201577) #144 不再把 `suppressed` 当隐藏标记。同步后先复现一项失败：旧测试期望排除的 `suppressed=empty_answer` 文章实际可见。修正保留这篇数据，并加入遗留 `source_paused` 的可见断言；`hidden=short_post`、读者帖、评论壳和重复版本仍排除。分别走真实候选和强制补齐区，验证来源、作者、日期、计数和两篇一页的完整顺序。分页算法本身无需为这条新规则增加分支，沿用主线的共享可见性条件。

- 完整本地单元及集成测试重新运行：**613 + 121 = 734 项通过，0 失败、0 跳过**，约 58.9 秒；只有原有 TestClient 弃用提示。总数变化来自主线增删测试及本次参数化，未删测试、降低门槛或改基线。
- ruff 和相对已同步 main 的 `git diff --check origin/main` 通过。
- 重新生成 1,500 篇 / 10,463 块语料，原有检索基线通过；七个文章搜索用例前 20 篇准确率均为 100%，问库平均准确率约 84%。新主线的翻译及 OCR 整理集成测试也包含在完整套件中，全部使用合成数据和本地替身。
- 继续使用独立容器 `research-pagination-db-20261030` 的 55432 端口，显式指定 `research_test`、`research_eval` 和 `research_eval_pagination_before/after`；未连接默认 5432 库、mini 或生产页面，未运行付费模型、真实翻译或识图任务。

| 合成规模 | 当前主线可达 → 修复后可达 | 主线请求中位 → 修复后请求中位 | 修复后单次取文最多 |
| --- | --- | --- | --- |
| 6,001 篇 / 8,301 块，单泛词 | 300 → 6,001 | 41.46 ms → 59.95 ms | 400 块 |
| 24,001 篇 / 26,301 块，两个 AND 词 | 2,300 → 24,001 | 296.17 ms → 427.52 ms | 2,400 块 |

两组修复后均无重复、遗漏或意外文章，计数、不同分页大小的顺序和末尾空页检查通过。主线/修复版分别遍历 7/65、27/245 次调用；大规模单次最高耗时 711.12 ms。主线简化可见性规则后小规模耗时大幅下降，不能继续拿首次的 2,235 ms 当当前对照；新结果表明完整覆盖有额外 SQL 开销。以上均为单机合成测量，真实模型、halfvec 和生产延迟仍未验证。

## 首次验证（历史快照）

- main `d88b602` 加本修复的本地验证：ruff、`git diff --check` 通过；**625 项单元测试 + 120 项集成测试全部通过，无跳过**。未改测试门槛或基线。
- 1,500 篇 / 10,463 块原有检索评测通过；七个文章搜索用例前 20 篇准确率均为 100%，问库平均准确率约 84%，准确率/召回率未低于已有基线。这里只验证合成语料，不代表真实语义质量已达标。
- 新增回归覆盖：2,407 篇全量遍历、长文挤占、同分/同日期稳定次序、跨区分页、无重复漏项、空页/越界/零长度页、来源/作者/公司/日期过滤、公司与词混合 OR、隐藏和评论壳、重复主版本、原文/译文及锚点、标题命中、无块文章、短语/排除词、语义提升去重及部分命中边界。
- 所有数据均在本次新建的 `research-pagination-db-20261030` 容器、宿主机 `127.0.0.1:55432`；只使用 `research_test`、`research_eval` 和 `research_eval_pagination_*` 合成库。原有 5432 容器和默认库未用于测试。
- 环境：Python 3.12.14、PostgreSQL 16.15、zhparser 2.4、pgvector 0.6.0。真实模型、halfvec、Mac mini 与生产查询延迟未验证；没有付费调用。

分页规模和耗时的原始汇总保存在 [证据 JSON](evidence/search-pagination-2026-10-30.json)。脚本按所有可翻页结果逐页遍历，再核对不同分页大小和边界，测量每次取回 Python 的正文行数。改前和改后在同一机器串行运行，均为本地合成数据；耗时不是线上 SLA。


| 合成规模 | 旧版可达 → 修复后可达 | 旧版请求中位 → 修复后请求中位 | 修复后单次取文最多 |
| --- | --- | --- | --- |
| 6,001 篇 / 8,301 块，单泛词 | 300 → 6,001 | 2,235 ms → 63 ms | 400 块 |
| 24,001 篇 / 26,301 块，两个 AND 词 | 2,300 → 24,001 | 294 ms → 416 ms | 2,400 块 |

两组修复后均为零重复、零遗漏，计数和换分页大小后的顺序正确。中位数覆盖各自可达页：小规模旧/新分别 7/65 次调用，大规模 27/245 次；大规模新增的完整覆盖有额外 SQL 开销。补齐页排除已排名文章使用文章号子查询，避免预备语句通用计划对长数组逐行比较。性能比较的旧代码是 `15a0469`；后续 main 的 #139、#140 没有改变检索算法；已同步到 #140 的 `d88b602`，重跑完整测试和原有检索评测。旧快照的验证数也保留在证据 JSON。

## 在另一台机器复现

在独立 checkout 中安装锁文件依赖。准备一个单独的 PostgreSQL 实例，含 zhparser 和 pgvector，端口例如 55432；可用仓库的 `docker/postgres/Dockerfile` 构建专门测试镜像。只在这个实例里创建 `research_test`、`research_eval`、`research_eval_pagination_after`，不要把下面 URL 换成正式库。

```bash
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps -e .
ruff check .
DATABASE_URL=postgresql://research:research@127.0.0.1:55432/research_test \
  pytest tests/unit tests/integration
python tools/retrieval_eval/gen_corpus.py --articles 1500 \
  --url postgresql://research:research@127.0.0.1:55432/research_eval
EVAL_URL=postgresql://research:research@127.0.0.1:55432/research_eval \
  python tools/retrieval_eval/evaluate.py --check tools/retrieval_eval/baseline.json
python tools/retrieval_eval/pagination.py \
  --url postgresql://research:research@127.0.0.1:55432/research_eval_pagination_after \
  --articles 6000 --check --json /tmp/pagination-small.json
python tools/retrieval_eval/pagination.py \
  --url postgresql://research:research@127.0.0.1:55432/research_eval_pagination_after \
  --articles 24000 --query 'paginationneedle capacityneedle' \
  --check --json /tmp/pagination-large.json
```

分页脚本强制显式 URL，且库名必须以 `research_eval_pagination` 开头；会清空该库的文章，再造指定数量的短文和一篇 2,301 块的长文。向量召回替换成本地空实现，保证不调用外部模型。小规模用单个泛词，大规模用两个 AND 词，后者超过 20,000 块索引排名窗口。

比较当前主线时，把 main `87e6494` 解包到独立目录，用 `PYTHONPATH=<旧目录>/src` 运行本 PR 的分页脚本，使用另一个 `research_eval_pagination_before` 库；主线预期不满足 `--check`，测量时可不传它来保存失败指标。首次历史测量使用的是 `15a0469`。不要修改原有 `baseline.json` 来抹平差异。

## 合并与后续边界

本次授权的发布范围仅限草稿 PR，未合并、上线、操作 mini 或生产数据。没有 SQL 迁移、回填、重算或额外上线命令。以上验证均为本地隔离环境结果；PR 是否已创建、远端提交和实际检查状态以 GitHub 记录为准。CI 配置会跳过草稿 PR 的 Tests 作业，但实际未运行、跳过和通过是不同状态；不以本地测试代替远端检查，也不为触发 CI 转成待审。

语义、只存在于 OCR 块中的匹配、部分词匹配仍使用有限候选；本次完整性承诺针对文章索引全部命中。补齐区按时间排序，不宣称全局最相关排序。offset 接口不提供跨导入、重算统计或模型更新的冻结快照。#99 的通用重排/训练工作仍独立，本次没有改动其分支或合入其代码。

后续验收可只读检查 `/api/v1/search?q=<常见词>&limit=100&offset=...`：`listed == total`，沿 `next_offset` 到底，全命中区文章数与总数一致；固定数据条件下无重复或缺页。真实库结果需要维护者现场确认，#126 里的腾讯 1,948 / 680 是历史案例，不是本次线上测量。

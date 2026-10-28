# 冻结的片段搜索验收

所有正文均为原创合成测试，不是生产资料，也不是有关公司的事实资料。实现者已经读过两套语料；
`split` 中的 holdout / new_blind_holdout 仅保留当时的划分，不能把后续结果称为新的盲测。

`original.jsonl` 和 `heldout.jsonl` 是父线程完整转交的原文，首行为元数据，此后每行一篇文章。
恢复的 JSON 字段顺序、正文和标签都不改动，完整文件 SHA256 为：

| 文件 | 篇数 / 块数 | SHA256 |
| --- | --- | --- |
| `frozen_corpus.json` | 34 / 42 | `e230d58faa4c8282965f6054b39a5ff73bbbc0f8b8aca3fd0efd5498e1c2cc9b` |
| `heldout_corpus.json` | 19 / 22 | `5ab7ca21d9d310d944ad96c2d83bc11e390b16c08d4e1acc1bef5a11a7eb6ac2` |

评测器每次先核对冻结 JSON 的哈希。两套语料分别装载到不同的本机 `research_eval_*` 库；
`--url` 必填，连接前校验 URL，执行迁移和清理前再核对 `current_database()`。
脚本只清空这个显式合成库里的文章及其关联数据，不创建或删除数据库。禁止传入默认测试库、正式库或远端主机。

装载使用普通 `replace_chunks` 和 `rebuild_all`。每个冻结块分别使用 `合成段 1`、`合成段 2` 等不同 heading，
标题、顺序、发布时间、`raw_meta` 和来源遵照独立验收约定。不预先认共享片段为整篇重复。
运行期设产品已有的测试标志来关闭后台认重/摘要线程；向量服务关闭，额外拦截 urllib 和 Python socket 网络连接。
检索只接收普通文章和原始 query；标签及期望 evidence 不进入检索。

示例（需要已具备 zhparser / pgvector 的本机 Postgres）：

```bash
docker exec research-db-1 createdb -U research research_eval_acceptance_original
docker exec research-db-1 createdb -U research research_eval_acceptance_heldout
PYTHONPATH=src python tools/retrieval_eval/acceptance/evaluate.py \
  --corpus original \
  --url postgresql://research:research@127.0.0.1:5432/research_eval_acceptance_original \
  --label <完整提交SHA> --out /tmp/search-original-result.json \
  --html-dir /tmp/search-original-html --check
PYTHONPATH=src python tools/retrieval_eval/acceptance/evaluate.py \
  --corpus heldout \
  --url postgresql://research:research@127.0.0.1:5432/research_eval_acceptance_heldout \
  --label <完整提交SHA> --out /tmp/search-heldout-result.json \
  --html-dir /tmp/search-heldout-html
PYTHONPATH=src pytest tools/retrieval_eval/acceptance/test_evaluate.py -q
```

通过 `PYTHONPATH=<旧提交归档>/src` 切换旧版本时，仍执行这一份评测器及冻结文件，模板由该旧版本实际渲染。
保存的文章数据库 ID → 稳定 ID 映射用于核对；旧版第二条 `p.item-hit` 没有独立链接，只用 `found.passages` 对齐来源块号，
机制相关性的判断仍完全来自实际 DOM 的可见文本。

每张卡按 DOM 顺序展开，旧版每个 `p.item-hit`、新版每个 `blockquote.item-hit` 算一项。
HTML 实体解码后只规范空白，不在 `<mark>` 边界添加空格，链接按钮文字不算证据。
一个窗口至少显示同篇某个 grade2 块的一个 `evidence`，且必要上下文在同一卡、前 k 项内完整可见，才获信用。
每窗最多一分；同组重复不获第二份去重信用；分母固定 5 / 10，缺位计错。
主题覆盖还要逐一检查其 `topic_evidence` 是否可见；只显示了整块前半截不能借后半截标签得分。

报告同时保存普通可见 P、可见去重 P、主题覆盖、冻结实体误归计数/比例、重复计数/比例、前十项实际可见文本和链接。
错误主体沿用冻结 `wrong_entity_for`，不按实现结果临时改标。重复按冻结 `duplicate_group`，无重复组时按稳定块 ID；
上下文重复可据实际显示的完整块被记入。比例以实际返回项为分母，故必须同时比较计数。
延迟为实际热路由 20 次的 p50 / 最近秩 p95，不包含 DOM 计分时间；宿主机无独占保证，不能单凭小幅变化作生产性能结论。

`--check` 只执行冻结的原三题门槛：每题去重可见 P@5 ≥ 4/5，Costco 主题覆盖 ≥ 3/4，另两题 ≥ 2/3，
前五实体误归和重复计数均为零。结果会先保存，再在未达标时非零退出。它不替代仓库原检索基线，也不证明真实语义模型效果。

`results/` 中 base `258507a` 与旧 PR head `a062e28` 的结果均来自经过文件比对的精确提交归档；
`working-r2` 仅是尚未提交的中间工作树快照，不是最终 head，也不表示搜索验收通过。

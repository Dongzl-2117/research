# 通用排序与偏好标注接口 v1

状态：实现接口与可复跑闭环，不宣称已有泛化模型。基线 main 为
`a7a4bb6bb0e93633f3e1fab042f8b111a006abe3`，沿用 draft #99 的局部证据和版本约束。
无真实双语模型；旧403下载不重试，不新增付费服务、凭据或生产全库向量写入。

## 最小接入点

`researchlib.ranking` 导出不可变 `RankQuery`、`RankCandidate`、`SourceFragment`、`RerankScore`。
词法/语义提供者都实现 `RecallProvider.collect(query, limit=...)`；候选合并后必须通过实体、可见范围和原文版本约束。
`Reranker.score(query, candidates)` 返回每个候选的 `candidate_id, score, window_hash`，只能给当前窗口打分，不能改正文或来源。
模型身份使用 `model_id/model_revision`。NaN、遗漏、重复、未知ID、错误window_hash或运行超预算都不局部混用结果。
失效候选必须被删除，不能因重排失败回退成过期原文。模型分数不自动意味着概率、回答正确或已满足全部主题。

默认不启用重排；无模型仍保留当前检索与可见降级标识。人工规则负责不可让模型越过的约束，学习排序单独维护。
局部双语/交叉编码重排器接入此接口；本轮没有下载、调用或声称测过真实重排模型。

## 给标注模块的字段

以下是排序内部规范，不强制UI使用这些JSON键名。UI导出schema由独立任务制定；
训练入口将用独立adapter转换，候选快照和标签来源不丢失，不在retrieval里解析UI表单。

`RankQuery.to_record()`：

- `query_id, text, topic_text, entity_ids, scope, scope_hash, parser_version, topic_group_id`。
- `topic_text` 不含公司名；模型仍收到完整 `text`，不能把实体删除当作理解主题。
- `topic_group_id` 可在收集时为空，但训练/冻结划分前必须提供审定的主题族，不能仅凭随机query ID防泄漏。

`RankCandidate.to_record()`：

- `candidate_id, query_id, source_id, article_id, document_family_id, source_version, window_hash`。
- `title, heading, text, before, after, lang`（语言在各fragment内）、`url, published_at, entity_ids, entity_basis`。
- `fragments`：每段含 `role=center|before|after, ord, start, end, text, chunk_version, lang, anchor`。
  必要上下文也有块版本和精确原文范围。`text/before/after` 是只读派生值，不能与fragments不一致。
- `recall_channels, lexical_score, semantic_score, semantic_model` 仅说明召回来源；AI标签不能由这些分数冒充人工给出。
- 稳定候选ID包含查询、来源、原文窗口与版本，不包含学习模型分数；换原文或上下文产生新ID。

一个偏好JSONL记录（JSON键名为接口，不是示意中文翻译）：

```json
{
  "schema_version": "research-ranking-v1",
  "pair_id": "稳定配对ID",
  "query": {"query_id": "...", "text": "...", "topic_text": "...", "entity_ids": ["..."], "scope": {}, "scope_hash": "...", "parser_version": "research-ranking-v1", "topic_group_id": "审定主题族"},
  "a": {"candidate_id": "...", "query_id": "...", "source_id": "...", "article_id": "...", "document_family_id": "...", "source_version": "...", "window_hash": "...", "title": "...", "heading": "...", "text": "...", "before": "...", "after": "...", "fragments": [], "entity_ids": ["..."], "entity_basis": "...", "url": "..."},
  "b": {"与a相同的完整快照结构": "..."},
  "presentation": {"left_id": "a或b的candidate_id", "right_id": "另一个candidate_id", "presented_at": "2026-10-29T00:00:00Z"},
  "label": {"choice": "a_better", "source": "user", "rater_id": "不含真实姓名的标注者ID", "created_at": "2026-10-29T00:01:00Z", "model_id": null, "model_revision": null, "prompt_version": null}
}
```

A/B为稳定候选槽位；UI随机左右位置并保存映射，再将左/右选择转换回A/B，不能直接把左当作A训练。
五种 `choice`：`a_better / b_better / tie / both_irrelevant / cannot_judge`。
`source`：`user / ai / synthetic`。AI弱标必须记录模型及版本、prompt版本；人工金标来自可信用户操作，不能接受客户端自称user。
示意JSON只展示字段，不是可训练样本；真实记录必须包含完整有效的两份窗口和来源版本。

候选正文、上下文和来源当作数据，不执行其中的指令；rater_id不用真实姓名，真实库导出文件不提交公开仓库。
本模块校验记录一致性，不能仅凭JSON声明证明某人确实标过；标注端需保留可信操作来源。

## 数据与学习边界

默认训练仅使用可信user记录；AI弱标必须显式纳入且独立计数，不能作为人工验证/测试真值。
演示数据标synthetic，显式允许后才训练，成绩只说明程序能运行。tie/both_irrelevant/cannot_judge必须保存；
简单pairwise基线仅从明确A/B偏好学相对顺序，不把优胜者硬转成“绝对相关”，也不谎称已学会无答案判断。

训练/验证/测试先按公司族、主题族、文档族与近重复关系分组再冻结。相同文档的不同版本、相同/近重复窗口不能跨组。
严格联合冷启动划分若因公司/主题相连而只剩一个组件，必须报数据不足，不能退成随机按段切分。
冻结清单记录输入哈希、分组策略、时间、各split及标签来源数量；验证/测试标签不参与训练。
公司/主题冷启动和无答案集分别报告，不能只测少数熟题。时间字段保留以便后续时间外验证，不凭随机拆分冒称时间泛化。

数据不足时，先以现成重排模型在未见题上建立零训练基准，成本是接入、运行延迟和评测；
微调另需足够多样且无泄漏的可靠偏好、版本管理、训练资源及回归成本。没有前者证据或标签规模时，
训练模型不是目标。CPU线性pairwise基线只验证收集→划分→训练→重排→评测的闭环，无法凭词法特征获得任意双语理解。

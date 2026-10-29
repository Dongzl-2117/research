# 通用排序闭环与交接

本轮提供能从偏好数据训练、验证、替换排序器的入口。它没有证明真实库的任意主题语义检索已经做好。
原有公司语料共现先验仍是词法降级；CPU baseline 只有查询与窗口的交叉统计特征。
无真实双语编码器/交叉编码重排器，本环境此前官方模型请求403，未重试、换路下载或调用付费服务。

当前基于 main `7771a76`；#99 继续保持 draft，#115 提供独立本机标注服务。
两分支没有互相复制模块或合并到 main。#73 的部署/身份门禁未整体恢复；本轮无迁移、无默认生产写入。

## 架构与目前缺口

1. 现有解析器保留查询、公司实体与范围，词法召回、可选已有向量召回及公司上下文分别保留来源。
   `RecallProvider`/`collect_hybrid` 可按RRF融合独立提供者；实际库入口 `collect_search_candidates`
   复用当前检索，标注默认 `use_semantic=False`，不会静默请求向量服务。
2. 模型只收到局部中心片段、必要上下文和来源快照。范围可见性、局部实体依据、当前文章/译文/块版本
   在评分前后检查；分数不能增添候选、替换公司或改写引用。已有实体规则有其召回边界，模型接口不解决实体解析本身。
3. `Reranker.score(query, candidates)` 是替换点，可接真正联合阅读查询与候选的本地交叉编码模型。
   本轮的 `LinearRanker` 是22项固定交叉特征的相对logistic基线，使用完整query，不含公司/主题ID预测项或答案词表。
   训练与手写降级规则分开，不把旧总分训练成“真值”。UI无召回分时保留缺失，不能伪造语义分。
4. 线上调用必须显式传入模型，默认不变。NaN/缺失/重复/未知候选/错窗口/失败时整批回退；并发变更的原文即使失败也删除。
   候选最多100个。预算为同步返回后的检查，并非可杀死任意Python调用的硬超时；未来模型adapter需自身限时。
5. `load_reviewed_ranker` 只加载精确模型版本及人工审阅声明，默认关闭；纯合成训练权重不能从该入口上线。
   review JSON 不是独立人工审阅的自证。验证/测试结果和训练记录必须交给维护者实际核对，不能由训练命令自动批准。

标签不足时应先测现成多语言重排器的零训练效果，再决定微调。前者需要可用权重、CPU/内存/延迟预算与真实留出评测；
后者还需要足够可靠、多样、无泄漏的偏好和维护成本。当前只有流程样本，训练不是为了宣称已得到好模型。

## PR115 数据接口与provider

持久化唯一格式是 PR115 `schema_version:1` 的 `manifest|task|presentation|label_event` JSONL。
`ranking/relevance_adapter.py` 调用其真实 `read_jsonl/validate_records`；没有该模块时报清楚前提，不另建平行格式。

- 左右是随机展示位置；adapter按`winner_id`对应候选ID恢复A/B。
- 每任务、标签来源、标注者取最大revision。撤回排除；tie/neither/skip/uncertain独立统计，后两者不训练。
- human→user，ai_weak→ai。material=synthetic/private_corpus另列；合成的人类标签仍须显式允许。
  默认不训练弱标，显式`--ai-weight`上限0.25；AI弱标不会出现在人工金标准确率内。
- 原文hash由UI校验。source_id缺失保留unknown；历史窗口没有原块偏移时使用window坐标、空偏移，不假造块0位置。
- 权威冻结先连通共享实体、主题、query、文章、文档族、内容与近重复。训练侧再补充审计，不改split或seed。
  新任务桥接已有多个组会被拒绝。连通巨组/缺holdout/无人类标签均报告，不拆组规避。
- 训练只取train；再次evaluate要求相同权重版本、manifest与train事件/窗口快照的训练记录。
  这些记录可审计，不能抵御操作者伪造整个文件，不能替代可靠标注来源。

`build_library_provider(connection_factory=..., sources=..., public_base_url=..., topic_resolver=...)`
直接符合PR115 `provider(*,query,scope)->Task`。每次用操作员提供的新连接开启只读事务，检查`transaction_read_only`；
不调用`prepare`、迁移、同步或模型。只允许配置且启用的来源，携带实际source_id、原文版本、准确窗口与原站链接。
主题族必须由维护的resolver给出；缺失就拒绝，不能将任意查询哈希当成“未见主题”。不足两段候选不制造答案。

真实库连接与topic resolver没有在仓库默认配置；实际操作员需检查目标、只读角色和主题族后显式组装provider，
再经PR115 `serve --provider module:function`接入。本轮只在全新本机合成eval库验证，未运行用户库。
真实材料、标签、导出及权重可能带私有信息，保存在被忽略的`data/`或独立目录，不提交公开仓库。

## 复跑闭环

当前两PR可在各自worktree临时组合导入，检查工具只将PR115目录加到此次进程的包搜索路径，不修改已安装模块。
先审阅两分支代码；命令不拉取模型。用新目录保留历史产物：

```bash
git fetch origin codex/general-relevance-annotations-20261029
git worktree add --detach /tmp/research-relevance-ui c63565569d2a71df667f75fdac68e1265de736d8
PYTHONPATH=src python tools/ranking/check_ui_integration.py \
  --relevance-root /tmp/research-relevance-ui \
  --output-dir /tmp/ranking-loop-run-1
```

该命令使用真实Store/随机展示/事件/HTTP导出，检查hash篡改拒绝、候选身份、修订、撤回、unknown来源、
弱标隔离、默认不训练、训练记录门禁、冻结train学习与validation/test评估。
可选`--database-url postgresql://...@127.0.0.1:5432/research_eval_<新库名>`检查真实检索→provider→UI生成。
它只接受本机research_eval_*，要求库为空，拒绝清空已有文章；测试会写原创合成样本，绝不能指向用户库。
该协议检查用模拟private_corpus类别验证UI接口，和学习fixture完全隔离，不产人工金标。

两模块都安装后，离线真实导出可用：

```bash
PYTHONPATH=src python -m researchlib.ranking check --input data/relevance/export.jsonl
PYTHONPATH=src python -m researchlib.ranking train \
  --input data/relevance/export.jsonl \
  --output-model data/relevance/weights-v1.json --report data/relevance/train-v1.json
PYTHONPATH=src python -m researchlib.ranking evaluate \
  --input data/relevance/export.jsonl --model data/relevance/weights-v1.json \
  --training-report data/relevance/train-v1.json --report data/relevance/holdout-v1.json
```

只有合成流程检查才增加`--allow-synthetic --ai-weight 0.25`。文件默认0600且不覆盖；命令不生成review或修改应用配置。
模型可在隔离实验中显式传给`search_articles(...,reranker=model,use_semantic=False)`核对真实窗口顺序；
正式选择与启用需另行可靠评测和审阅，不凭同一训练集成绩自动上线。

## 实际证据及不能推出的结论

实测摘要见[JSON证据](ranking-pipeline-evidence.json)。完整可复跑输入为`tools/ranking/fixtures/pairs.json`，32个虚构公司/主题/文档族，另含无答案、相当、跳过、不确定、撤回。
标签全是**程序化模拟弱标**（不是实际LLM判断，也不是人类标注），用于协议测试。固定seed产生32组，train/validation/test=22/3/7；
一条train标签撤回，train输入21对、明确方向16对。未按测试成绩换seed或修改文本/标签。

| 留出集 | 方向标签 | 固定中心词法baseline | CPU学习baseline | 评分p50/p95（毫秒） |
| --- | ---: | ---: | ---: | ---: |
| validation | 3 | 0/3 | 3/3 | 0.100 / 0.105，3次 |
| test | 5 | 0/5 | 5/5 | 0.092 / 0.122，7次含非方向样本 |

manifest=`087ff152e94e03416a028cf2244b0af6e3baea92122203f7b5ddc3a416b6401d`，
模型revision=`e672185ddf77ac8d62ffd3969f4008e40caaac7c6a66ef747fc0918ea65c4abc`。
两次导出随机左右和事件ID不同，训练后权重revision相同；CLI训练/重评估也已实跑。
人工方向标签为0，人工准确率null；test另有neither=1、tie=1，拒答准确率null。
这些例子的正负文风/长度有人工构造差异，模型可能学到这些浅层线索；实体/主题ID未重叠不消除生成模板分布偏差。
**3/3、5/5只证明管道运行，不能称真实未见公司/主题泛化有效，也不是P@5。**

真实PG检索provider→HTTP generate在隔离空库通过，连接只读、已知来源/版本保留、其他公司候选未进入对比、语义调用0。
并发原文更新、无效模型输出、预算回传超时都有真实数据库回归。

默认不开启学习模型的冻结五题回归可见去重P@5仍为5/4/5/5/5，主题覆盖完整，前十误归/重复为0。
这是已有#99降级排序的保留证据，不能算新模型效果。全量测试及1500篇旧基线、最终head CI状态写在PR描述；
draft导致Actions跳过时必须写跳过，不能记通过。

仍需可靠用户标签、真实库候选分布、真正可运行多语言模型和独立未见题/无答案验收，才能比较现成模型、规则和微调，
并报告实体范围、精确引用/上下文、主题覆盖、多样性及端到端延迟。偏好排序胜率不能替代这些指标。

# 通用查询相关性偏好标注：本机最小模块

模块为后续通用排序收集“同一查询下两段原文哪段更相关”的可回放标签。适用于不同实体和主题，不含公司/主题专用词表。不生成答案、不自动打 AI 标签、不训练模型，也没有证明排序泛化效果。

这是小批量标注工具：每次操作校验独立标注快照，分组的近重复检查随任务数作两两比较。不要把整个资料库的所有窗口对一次导入；大规模队列与索引留待后续独立优化。

本次从 main `a7a4bb6` 开始，提交前同步到 `7771a76`（含 #114 译制对齐修复）。开工时 #99 仍为未合并的 draft，head `47c51c15f951c7d0181224e19af2ff8f67f8032f`，与 main 有冲突；没有把其冻结五题成绩当作通用算法验证。本模块不修改 `retrieval.py`、检索分数或其业务访问路径。实际库候选 adapter 和学习排序由独立工作接入。

## 怎样在本机打开

以下是**运行命令，不是已经部署的在线地址**。在 mini 或个人电脑上用独立 checkout/worktree；不要在 mini 自动上线使用的 `~/research` 工作树切分支。Python 3.12+，依赖沿用项目锁文件，不增加依赖：

```bash
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m researchlib.relevance demo --db data/relevance/demo.sqlite3
.venv/bin/python -m researchlib.relevance serve \
  --db data/relevance/demo.sqlite3 --annotator-id local-human --port 8099
```

进程成功启动后，在**同一台机器**的浏览器打开 `http://127.0.0.1:8099/annotate`。不需要启动主站、Postgres 或任何模型。关闭服务按 Ctrl+C。`demo` 创建 3 个原创虚构任务，页面及来源页明确标“合成”；它们不是用户真实库，也不响应任意输入查询。

判定包含 A 更相关、B 更相关、相当、都不相关、跳过、无法判断。“相当”用于两段都有帮助、相关程度相近；两段都不能回应问题时选“都不相关”。保存后先显示已保存的条目，再点“下一条”，避免快速连点误标下一任务。“纠正上一条”可以改判或撤销；撤销追加最新 revision，保留审计历史，再将该任务放回待标队列。

## 独立数据与访问边界

- 只读/写 `--db` 显式指定的独立 SQLite 文件。没有 Postgres 迁移、现有业务表修改、Web 启动维护或重算。
- `init` 只创建新文件，拒绝覆盖任何已有文件；`serve` 只检查已有库，不自动建表。新库与 CLI 导出默认权限 0600。建议全放入已被 `.gitignore` 忽略的 `data/relevance/`。
- 标注路由只挂在独立 `create_annotation_app`，主站没有 `/annotate`。服务固定监听回环地址，关闭 uvicorn 代理头信任；路由同时检查真实 peer、Host、Origin、跨站状态和代理/Cloudflare 头。所有写请求还需本机页面生成的 CSRF 凭据。
- 标注身份来自 `--annotator-id`，请求中不能指定用户、source 或模型。它标识当前本机操作员，**不是登录认证或“人类本人”的身份证明**。同一系统账户上能读取文件/运行 CLI 的程序仍在本机信任边界内。
- 本轮不设置 Cloudflare、Tunnel、登录、远程凭据或部署。需要远程多人标注时，必须另外实现认证/身份绑定，并经明确授权部署；不能把本机身份模式直接公开。私有任务及标签也不能通过公开 PR、附件、日志或截图发布。
- 本模块不读取 `.env` 或 `DATABASE_URL`，不导入主站、业务库、retrieval、LLM 或 embeddings。无 provider 时生成候选接口返回 503，不把固定 demo 当真实检索。

## 候选 provider：与排序实现的对接点

```python
from pathlib import Path
from researchlib.relevance.schema import Task
from researchlib.web.relevance import create_annotation_app


def library_pair(*, query: str, scope: dict) -> Task:
    # 在独立 adapter 中读取明确授权的库快照/只读连接，调用选定的候选算法。
    # 提供两段实际原文及必要上下文，并填真实稳定 ID、原文版本、算法/模型版本。
    # 不在这里生成答案、改写原文或默认启动收费模型。
    raise NotImplementedError("替换为实际库候选 adapter")


app = create_annotation_app(
    Path("data/relevance/private.sqlite3"),
    annotator_id="local-human",
    candidate_provider=library_pair,
)
```

接口固定为 `provider(*, query: str, scope: dict) -> Task`；同步函数在 FastAPI 工作线程执行。也可用命令 `serve ... --provider your_package.your_adapter:library_pair` 显式加载可调用函数。模块本身没有默认生产 adapter；上例只是接口说明，不可直接生成候选。

先显式初始化空数据集，再提供 adapter：

```bash
.venv/bin/python -m researchlib.relevance init \
  --db data/relevance/private.sqlite3 --dataset-id private-relevance-v1 --seed frozen-20261029-v1
.venv/bin/python -m researchlib.relevance serve \
  --db data/relevance/private.sqlite3 --annotator-id local-human \
  --provider your_package.your_adapter:library_pair --port 8099
```

配置 provider 后，页面才显示实际库查询表单。`POST /api/relevance/generate` 接受 `{query, scope}`；provider 必须返回相同规范化 query/scope、`material="private_corpus"` 的 Task。任务入库时冻结分组，随后出现在待标队列；重复 query/同一证据对不重复创建。返回任务 ID 与冻结组/集合。来源 URL 应为可打开的原始文档 HTTP(S) 地址，不能只给局部文件路径或依赖本进程不存在的主站相对路由。

实际库访问、来源权限、只读连接、运行模型的选择/授权均由 adapter 负责。仅设置 `--provider` 不代表本模块替它提供只读保证，也不自动授予付费或生产写入授权。

## JSONL v1 契约

每行 `{schema_version:1, record_type, data}`；未知字段/版本、错误 hash/ID/映射、重复冲突与不连续修订会被拒绝。完整可运行样本在 `tools/relevance/demo.jsonl`，没有私有材料或预设标签。

| record_type | data |
| --- | --- |
| `manifest` | `schema_version, dataset_id, seed, grouping_version, manifest_id` |
| `task` | `task, group_id, split, manifest_id` |
| `presentation` | `presentation_id, task_id, annotator_id, left_id, right_id, created_at` |
| `label_event` | `event_id, request_id, task_id, presentation_id, source, decision, winner_id, revision, created_at, split, group_id, manifest_id` |

`task` 的字段：

```text
query, query_id, scope, entity_ids[], topic_ids[],
material: synthetic | private_corpus, task_id, candidates[2]
```

候选字段：

```text
article_id, chunk_id, window_id, document_family_id, content_version,
source_id?,
article_title, heading, text, context_before, context_after, source_url,
content_hash, candidate_id,
algorithm: {name, version, model_name?, model_version?}
```

`source` 为 `{kind:"human"|"ai_weak", labeler_id, model_name?, model_version?, prompt_version?}`。AI 弱标必须注明模型名；版本/提示版本可选，建议都填；human 不接受模型来源字段。浏览器写接口只能创建服务端配置身份的 human 标签。AI 弱标仅通过显式离线工具/API 生成后导入，不在本模块内自动调用模型。

候选的可选 `source_id` 表示原文来源身份，缺失或 null 规范化为 `unknown`；不从 scope 或 algorithm 猜来源。它是独立来源元数据，不进入展示内容 hash。

`decision` 的枚举为 `left/right/tie/neither/skip/uncertain/withdraw`。方向偏好的 `winner_id` 对应实际候选 ID；其他枚举必须为 null。消费端不能把 left/right 直接当 `task.candidates` 的数组序号。所有时间须有时区。

身份计算使用 UTF-8、键排序、无多余分隔空白、不转义非 ASCII、禁止 NaN 的 canonical JSON，再做 SHA-256：

- query 先折叠首尾/连续空白；`query_id = hash({query, scope})`。
- `content_hash = hash({article_title, heading, text, context_before, context_after})`，绑定实际展示的完整文本，不含算法分数。
- `candidate_id = hash({article_id, chunk_id, window_id, content_version, content_hash})`。
- 候选按 candidate_id 排序；`task_id = hash({query_id, candidates:[两个 candidate_id]})`。左右顺序另存 Presentation，不改任务身份。
- 上述 ID/hash 可省略，由 Pydantic schema 自动补齐；传入时必须一致。相同证据对的元数据冻结，版本/原文变化需新内容身份，不静默覆盖。

写标签接口为 `POST /api/relevance/labels`，只接受 `presentation_id, decision, expected_revision, request_id`。相同请求 ID 与内容重复提交返回原事件；重用请求 ID 执行另一操作，或版本落后，返回 409。SQLite `BEGIN IMMEDIATE` 保证并发比较/写入原子性。重复导入同一记录幂等，冲突导入整体回滚。

## 分组、冻结与覆盖不足

`split` 为 `train/validation/test`。同实体、主题、文章、文档族、内容 hash，或满足近重复规则的证据通过连通关系分组。`entity_ids` 可为空，`topic_ids` 至少一个；它们应来自稳定命名空间。`document_family_id` 须覆盖转载、修订、译文等同族版本，不能为方便划分而随意换 ID。

近重复规则是原文中心段落经 NFKC、大小写与非字母数字归一化后的字符 5-gram：集合相等、Jaccard 至少 0.85，或较短集合至少 20 项且包含重叠至少 0.9 时连接。它是有界的文本重复保护，不能证明所有语义改写都已找到；真实文档族元数据仍需维护者核实。

首次导入先对整批形成连通组，再由 seed 与 group_id 的 hash 分配 80/10/10。增量任务只能继承已连接的单个冻结组，或新建组；连接多个已有组会拒绝，不能移动已有数据或自动重新分组。完整回放保留导出的分组和 manifest；相同初始批次不受输入行顺序影响。增量批次边界是冻结历史的一部分，不能删除任务后重新抽 seed 追求好看的成绩。

保守分组可能把同一广泛主题连接成一个大组。**不会静默拆开**：`report`、`check`、`import/demo`、CLI 导出、Web 状态及导出响应头都会报告单组坍缩、最大组占比过高、缺失集合、人类方向 holdout 不足、仅有合成材料等情况。3 个 demo 任务实际全分到 train，因此它没有可用 holdout，这是预期显示的告警。若真库也缺 holdout，应追加独立实体/主题/文档族并人工复核；不能报告训练集分数为泛化成绩。

CLI 导出附带 `<文件名>.report.json`；它是派生报告，JSONL 本身仍只有上述四类记录。Web 导出带 `X-Relevance-Split-Warnings` 机器可读代码及指向 `/api/relevance/report` 的 Link。报告只统计最新修订的人类方向标签，弱标不补成人类真值。

## 校验、导入、导出与学习侧消费

```bash
# 检查公开合成 fixture，不写库、不产生标签
.venv/bin/python -m researchlib.relevance check tools/relevance/demo.jsonl
# 恢复到独立新库，或向相同 manifest 的库幂等导入
.venv/bin/python -m researchlib.relevance import \
  --db data/relevance/replay.sqlite3 --input tools/relevance/demo.jsonl
# 默认只导出 human 事件，保留其完整修改/撤销链及任务/展示映射
.venv/bin/python -m researchlib.relevance export \
  --db data/relevance/demo.sqlite3 --output data/relevance/human-v1.jsonl
.venv/bin/python -m researchlib.relevance check data/relevance/human-v1.jsonl
.venv/bin/python -m researchlib.relevance report --db data/relevance/demo.sqlite3
# 显式全来源审计；AI 弱标不能混成人类真值
.venv/bin/python -m researchlib.relevance export \
  --db data/relevance/demo.sqlite3 --source all --output data/relevance/audit-v1.jsonl
```

CLI 导出拒绝覆盖已有导出或覆盖报告。导出包含原文与标注身份，所有私有导出及数据库仅留本地；不能提交公有 repo。Web 下载通过浏览器保存，文件权限由浏览器/系统管理。

学习侧应先校验完整包，再按 `(source.kind, source.labeler_id, task_id)` 取最大 revision，之后处理 withdraw。只用明确选择的来源/集合；方向偏好训练只取 left/right；tie 和 neither 的语义不同，不能自动都当 0.5；skip/uncertain 不能当负例。多个标注者可能有分歧，聚合政策应单独明确。评估保留冻结 validation/test，AI 弱标独立报告，并附覆盖告警。

## 启动与回退

没有新增业务数据库迁移；普通主站合并/启动不会启用本入口、创建标注文件或运行模型。本轮仅提交 draft，不转 ready、不合并/部署，也不重试 #99 的模型 403 或 ready 审批拒绝。

首次使用需要在选定机器拉取该分支、安装锁定依赖、显式导入/初始化独立库、运行独立服务；实际库还需实现并审核 provider/来源权限。长期后台运行、远程认证/入口和任何生产模型访问均未设置。

回退只需停止独立服务并回到旧代码；标签文件保留不删除。代码回退不会撤销已提交的标签、恢复其他数据库或更改生产状态。SQLite 模式版本未知时拒绝打开；未来若需升级，另写明确迁移，不在 serve 中隐式升级。

## 验证命令

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/unit
node --check src/researchlib/web/static/relevance.js
```

新增测试全用临时 SQLite/合成材料：来源上下文、两种随机映射、各选项、同对去重、并发/重试、过期 revision、修改与撤销、全历史导出/导入回放、弱标隔离、分组冻结/近重复/坍缩告警、访客/代理/CSRF/身份伪造拒绝，以及禁止导入业务库/模型模块的独立子进程。

可选浏览器验收脚本使用 `playwright-core` 和现有 Chromium；无需下载模型：

```bash
# 在独立工具目录安装 playwright-core，或使用本机已安装的包
npm install --prefix /tmp/relevance-browser-tools playwright-core
NODE_PATH=/tmp/relevance-browser-tools/node_modules \
  RELEVANCE_PYTHON="$PWD/.venv/bin/python" RELEVANCE_CHROMIUM=/path/to/chromium \
  node tools/relevance/browser_smoke.cjs
```

脚本自己创建临时合成库、启动回环服务，检查来源弹窗、刷新恢复、连续点击、tie/neither 区分、最新修订撤销和移动端布局，结束后停止进程并清除临时合成库。支持 `RELEVANCE_SCREENSHOT=/tmp/relevance-mobile.png` 保存合成页面证据；不对私有材料截图发布。

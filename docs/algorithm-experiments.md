# 算法实验台：本机与网站管理接入

这是站点所有者自己调参、用同一组问题比较两套结果的第一版。覆盖关键词搜索排序、问库选段与提示词、可选的两版回答及人工评价。不是随机流量实验，不会自动认定赢家或发布参数。

仓库内未发现可核验的所有者认证，且本次未核查线上代理认证。网站管理接入因此**默认关闭**：没有认证适配器时拒绝启用，普通主站没有管理入口。保留独立本机工具；普通启动不创建实验库、不改变默认搜索、不增加迁移、不调用实验模型。首次实际使用仍需所有者提供明确授权的离线库副本；本 PR 仅发布实现供评审，不读取生产数据、创建生产副本、合并或部署。

## 先用合成资料打开

Python 3.12+；沿用现有锁文件，无新增依赖。在独立 checkout 运行，不要改 Mac mini 自动上线所用的 `~/research` 工作树：

```bash
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps -e .
python -m researchlib.experiments demo --output data/experiments/demo.json
python -m researchlib.experiments init \
  --snapshot data/experiments/demo.json --db data/experiments/demo.sqlite3
python -m researchlib.experiments serve \
  --snapshot data/experiments/demo.json --db data/experiments/demo.sqlite3 --port 8100
```

启动后在同一机器打开 `http://127.0.0.1:8100/experiments`。默认没有任何生成适配器，运行对照不会调用 LLM 或向量服务。合成演示只含原创虚构人物和文章，页面会明确提示，不能用它宣称真实算法质量提升。

页面使用步骤：

1. 勾选问题，保留 A 为基线，在 B 调整参数；可用“更重视标题”“更重视完整命中”作起点。
2. 点“运行双方案对照”。在关键词搜索、问库检索、问库回答三个标签切换，查看原文和实际提示词。
3. 分别评价 A 更好、B 更好、相当、都不好、跳过或无法判断。理由随评价保存；改判或撤销保留修订历史。
4. 从历史下拉菜单恢复，或导出参数、完整结果及评价。刷新/相同参数重跑复用同一结果，不重复生成。

## 对真实离线副本捕获问题组

捕获只接受**显式本机** `research_experiment`、`research_eval`、`research_test` 库，不接受 `research`、远程主机、hostaddr、service 或 options 覆盖。使用本机离线副本与只读角色；名称/回环检查是额外保护，不能代替维护者核实它确为离线副本。不会读取 `.env`、`DATABASE_URL` 或自动调用 `db.prepare`。

连接前还会拒绝当前进程中的全部 `PG*` 环境变量（包括 `PGHOSTADDR`、`PGSERVICE`、`PGSERVICEFILE`、`PGSYSCONFDIR`、`PGOPTIONS` 及认证变量），不只校验显式 DSN。错误仅列变量名，不回显值；不会临时清空或修改全局环境。请在独立捕获命令的环境中移除这些变量，必要的账号、密码或凭据文件改由权限 0600 的 DSN 文件显式指定。连接地址固定为数字回环；`localhost` 固定使用 `127.0.0.1`，不依赖名称解析；端口固定为显式单个合法值，未提供时为 5432，拒绝端口列表。服务配置继承入口被拒后，不会读取其指定的服务文件。

先把明确目标写入私有的 `data/experiments/snapshot-dsn.txt`，例如 `host=127.0.0.1 port=5432 dbname=research_experiment user=experiment_reader`；文件权限设为 0600。认证由操作员配置，凭据不进仓库或日志。副本需已有当前 schema、zhparser 及文章块；捕获不创建扩展、迁移或补算块。

问题文件 `data/experiments/questions.json`：

```json
[
  {
    "id": "retail-mechanism",
    "query": "零售 会员制",
    "scope": {
      "source_ids": ["fangwei-mp"],
      "date_from": "2024-01-01",
      "date_to": "2026-12-31"
    }
  }
]
```

`scope` 另支持正整数 `company_id`、`article_id`、`tree_id`。未知字段、非法 ID、空来源和反向日期拒绝，不会静默扩大范围。每组 1～20 题；第一版要求明确主题词，不接受仅“总结一下”等概览。新增问题需重新捕获；没有把三题固定 demo 伪装成任意实际查询。

```bash
python -m researchlib.experiments capture \
  --dsn-file data/experiments/snapshot-dsn.txt \
  --sources config/sources.yaml \
  --queries data/experiments/questions.json \
  --output data/experiments/private-v1.json
python -m researchlib.experiments init \
  --snapshot data/experiments/private-v1.json --db data/experiments/private-v1.sqlite3
python -m researchlib.experiments serve \
  --snapshot data/experiments/private-v1.json --db data/experiments/private-v1.sqlite3 \
  --sources config/sources.yaml --port 8100
```

捕获在一个 `REPEATABLE READ READ ONLY` 事务中使用现有 `analyze`、`_candidates` 和范围 SQL，最后回滚；数据库本身也会拒绝误写。来源须同时存在于启用配置和副本启用记录中。隐藏、读者帖、评论壳、非指定重复文章及范围外内容按原查询约束过滤。候选、前后文、词频、公司提及深度、时间、来源元数据均冻结；整个快照 SHA-256 绑定内容版本，不仅绑定文章编号。

私有服务必须指定当前 `--sources`。启动及每个请求都检查快照涉及的来源仍启用且配置一致；停用或策略变更后读、写、导出均拒绝，需重新捕获。文章的隐藏、修订及版权状态反映的是**副本捕获时**，不是生产实时授权查询；撤回内容时应停止服务并重建副本/快照，不继续使用旧离线材料。文件仍在本机保留，不自动删除。

## 可以优化的内容与基线含义

| 参数 | 默认 | 范围 | 影响 |
| --- | --- | --- | --- |
| K1 | 1.2 | 0.1～3 | 词频饱和 |
| B | 0.4 | 0～1 | 段落长度归一化 |
| 标题词频权重 | 0.5 | 0～2 | 标题参与每段评分 |
| 标题额外加分 | 0.3 | 0～2 | 标题命中奖励 |
| 相邻词组加分 | 0.5 | 0～2 | 邻接词项奖励 |
| 覆盖率幂 | 2 | 0.5～4 | 完整命中偏好 |
| 问库最低相对分 | 0.12 | 0～0.5 | 相对榜首筛段 |
| 问库段数 | 24 | 1～80 | 最多原文段数 |
| 问库正文字符 | 24000 | 1000～32000 | 不截断原段的材料预算 |
| 每篇基准段数 | 3 | 1～6 | 沿用主站强长文最多 3 倍规则 |
| 附加回答要求 | 空 | 最多 2000 字符 | 添加到共用问库系统提示词 |

默认评分公式与主站相同，直接共用 `score_rows`、`rank_articles` 和 `select`，没有复制另一套排序器。`ScoreParameters` 为请求内不可变参数；没有修改 `K1/B/BUDGETS` 等进程全局配置。正常主站调用不传参数，原行为保持不变。

关键词候选与主站词法检索相同（每通道份量 2000），全部词命中的文章仍优先，文章内片段与主站相同方式去重/聚合。问库候选使用主站默认 llm 的 640/480 份量，两方案共用这个固定池。改变预算不会重新召回；不能据此评估召回模型、扩词、向量权重或 #99 学习排序器。前 20 和 Top 10 集合重叠不等于准确率。

问库实验先剔除单段正文就超过字符预算的候选，再调用共用 `select` 分配段数与累计字符预算；不会让超大高分段先占预算、事后删除又错失可用小段，也不截断原文来凑预算。

问库实验使用较小的单次材料预算与前后文，不调用生产问库执行器、不写 `llm_answers`，不走自动扩词、deep/map-reduce、全篇/目录特殊处理、无证据概览回退或重试。所以 A 的含义是“当前词法评分 + 实验预算”，**不等于线上完整问库端到端基线**。不在这里修改 #225 的检索修复或 #239 的聊天页面。

每条对照保存：快照 hash、问题/范围、两侧完整参数、实现源码 hash、原文/段号/引用/提示词、时间与人工评价。实现 hash 覆盖评分、问库提示、实验引擎、参数 schema 与内置模型适配器；更换这些实现会产生新对照 ID。相同问题/快照/参数/实现产生相同 ID，历史结果不覆盖。耗时只是本机回放参考，不代表生产延迟；首次计算结果含耗时，重复运行读取已冻结记录。

## 可选回答生成与费用

默认仅看原文及提示词，始终零模型调用。可从页面导出实际 messages，交给自选工具；未启用生成时不会接入主站凭据；显式选配站内模型见后文。

若希望在页面生成，操作员需显式提供一个**自己审核的** `module:function`：

```bash
python -m researchlib.experiments serve \
  --snapshot data/experiments/private-v1.json --db data/experiments/private-v1.sqlite3 \
  --sources config/sources.yaml --generator your_adapter:generate
```

适配器同步接口：`generate(*, messages: list[dict], max_output_tokens: int) -> dict`。返回字段为 `text, model, model_version`，可选 `input_tokens, output_tokens, cost, currency`；金额只有有币种时才接受，未知费用/用量保留 null。必须自行实现输出上限、HTTP 超时及真实版本回报；适配器内部调用及重试由其实现负责。不要在适配器导入时自动调用模型。另有内置的站内受支持模型适配器（见下节），也未在本次工作中调用真实模型或验证真实模型质量。

网页必须勾选当前题的数据传递/费用确认后才调用。实验台每题仅各调用两侧适配器一次；自身无扩词、分批、自动重试。自定义适配器每侧传 `max_output_tokens=1500`，完整提示词超 40000 字符或任一侧无证据时拒绝调用。主站完整问库可能另有扩词、分批和重试，**不能把“两侧”当作实际 HTTP 次数或据此估造费用**。

SQLite 先原子占位并保存已尝试侧，再调用适配器；`attempted_sides` 表示适配器尝试（含排队），不证明模型 POST 已发送、供应商已收到或已计费；并发和重复请求复用占位。每侧成功立即保存，部分失败保留成功侧与未知费用，不自动重跑。进程中断后 running 表示结果可能未知，不会因刷新再次调用；需要操作员核实适配器/供应商记录。没有自动清理占位或付费重试按钮。只有两侧成功才允许回答人工评价。生成内容仅作文本展示，原文和回答里的 HTML 不执行。

## 人工评价与现有标注工具

搜索、问库选段和回答分别记评价。“相当”“都不好”“跳过”“无法判断”“撤销”独立计数；B 偏好比例只在明确 A/B 方向题内计算，无方向评价时为 null。改判使用修订号与请求 ID，重复提交幂等，过期页面 409，旧事件完整保留。不会把弱标当人工真值、把未评价当负例、声称统计显著或泛化提升。每次调参都会看过这组题，需要另建独立问题快照再验证，第一版不自动划训练/留出集。

已有的 [原文相关性盲测](relevance-annotations.md) 继续独立使用。本实验是带方案名字的整组结果人工评价，不伪称原文盲测、也不生成或混写它的训练标签。#99 的重排/训练闭环未合并且冲突，没有复刻或自动加载其学习权重。

## 本机访问和回退

独立本机模式固定监听 127.0.0.1，关闭代理头信任；真实 peer、Host、Origin、跨站和代理头检查覆盖读写及下载。写请求需要本机页面 CSRF，正文上限 64KiB，静态资源白名单，CSP 禁止脚本注入与被框架嵌入。它是本机信任边界，**不是远程所有者登录**；不要映射到 Cloudflare Tunnel 或公开代理。远程使用需另做认证和部署方案。

新快照、SQLite 及 CLI 导出默认 0600，全部建议放在已忽略的 `data/experiments/`。Web 下载权限由浏览器决定，内容含原文、提示词和评价，只留私有目录。初始化及快照导出拒绝覆盖已有文件。停止进程即可关闭，回退代码不删除历史文件。实验不会将候选参数应用到线上；确认效果后由后续独立变更评审上线。

## 复跑验证

```bash
ruff check .
pytest tests/unit tests/integration
python tools/retrieval_eval/gen_corpus.py --articles 1500
python tools/retrieval_eval/evaluate.py --check tools/retrieval_eval/baseline.json
node --check src/researchlib/web/static/experiments.js
EXPERIMENT_PYTHON="$PWD/.venv/bin/python" \
  EXPERIMENT_CHROMIUM=/usr/bin/chromium \
  EXPERIMENT_SCREENSHOT_DIR=/tmp/algorithm-visual \
  node tools/experiments/browser_smoke.cjs
```

浏览器验收使用已有 `playwright-core`/Chromium，不增加项目依赖；仅创建临时原创合成快照和模型替身。数据库测试只使用 `research_test`；旧检索评测只使用独立 `research_eval` 合成库，基线不降低。

首轮本地结果（main 基点 `78dd8bedd8747fa49e8f7598148f10c45e86e1e8`）：完整单元＋集成 **1099 passed、0 failed、0 skipped**，其中新增实验单元 61 项、PostgreSQL 捕获集成 4 项。1500 篇／10463 块旧检索基线无退步；Ruff、JS 语法、diff 与锁定依赖检查通过。浏览器实际通过参数隔离、三个视图、评价连点/修订/撤销、原文安全显示、刷新恢复、键盘切换、显式无网络替身生成与导出，并核对 1440/1024/390 宽度无横向溢出。全部使用本机隔离 PostgreSQL、原创合成材料和模型替身，真实模型/向量调用为 0。

另在独立临时目录叠加 #225 的 `chunks.py`、`embeddings.py`、`retrieval.py` 源码补丁，能够干净应用；新功能 65 项测试及同一检索基线再次通过。这是针对性组合验证，不是 #225 的完整联合验收。#239 的问库聊天页面在首轮与本功能分离。最新网站管理接入会改动 app/routes/base 模板，尚未做 #235/#239 全分支组合测试，后续整合需按各自最终 head 复验。#99 保持独立未合并，现有 relevance SQLite/标签协议不修改。


独立复查补修：连接校验原先只看显式 DSN，遗漏 libpq 从 `PGHOSTADDR` 等继承目标的入口；选段原先在 `select` 后删超大段，会连带丢掉本可放入的小段。现在分别拒绝所有继承 `PG*` 配置并固定数字回环/单端口，以及先过滤超预算候选再分配预算。新增反例在旧实现上失败；回归不访问远端数据库，连接值仅使用回环，另以真实本机 `research_test` 验证实际连接地址。


本次补修后完整单元＋集成为 **1158 passed、0 failed、0 skipped**；实验相关测试共 124 项（119 单元＋5 本机数据库集成），本次新增 58 项单元反例和 1 项真实回环地址集成。新的单元反例在旧代码上失败，修复后全部通过。浏览器验收、1500 篇检索基线、Ruff 与 diff 检查再次通过；没有生产、远端数据库或真实模型访问。


## 站内受支持模型：现在可选配，无需自行编写生成适配器

本机工具可明确选择已有站内 DeepSeek/Grok 提供方：

```bash
python -m researchlib.experiments serve \
  --snapshot data/experiments/private-v1.json --db data/experiments/private-v1.sqlite3 \
  --sources config/sources.yaml --site-model
```

`--site-model` 与自定义 `--generator` 互斥。CLI 只读当前进程环境，不自动加载 `.env`；沿用现有 `RESEARCH_LLM_PROVIDER`、模型端点、DeepSeek key 或 Grok OAuth 配置，不创建凭据、不自动换提供方。构造/预览只生成计划，**不读取凭据或联网**；只有界面勾选当前题并发送生成请求才调用。网站模式则使用主站已有 Settings，但需额外模型开关。

`SiteModelGenerator` 复用现有模型/推理参数、认证和并发限制，单独采用短回答预算：每侧请求 `max_tokens=1500`，不改变主站其他调用的输出地板或强度。内置适配器必须收到服务端发送前授权回调；取得并发名额后先检查会话/来源，再准备凭据，准备结束后紧邻网络发送前再查一次。缺少回调则拒绝执行。客户端每组最多两次模型 POST，不自动扩词、分批、刷新重发、退避重试或扩大预算；禁止把原文和 Bearer 重定向到其他端点。响应大小封顶、只接受正常 `stop` 的非空完整正文。缺凭据、空正文、429/5xx、超时、截断和超限都失败并保留已占用记录。Grok 取现有 OAuth 凭据时可能需要认证刷新，属于认证流量，不是额外的模型整合调用。

界面显示具体模型、两次请求上限、每侧 1500 token、读写超时与每日额度；**费用未知不等于免费**。思考也可能消耗输出预算，短预算可能没有正文，失败仍可能计费。不估造金额；仅保存提供方实际回报的输入/输出 token，未回报则 null，真实权重版本为 unknown。60 秒是网络读写超时，不承诺绝对总时限或固定账单。

内置适配器默认按 UTC 日期每个实验 SQLite 最多 5 组，可由网站配置选择 1～20 组；占位、失败、未知结果都计入，SQLite 原子检查阻止并发越额。操作员换文件/快照会形成另一个独立账本，不是供应商账户的全局金额限制。相同问题/配置生成不自动重跑。调用计划 hash 绑定当前模型/端点/预算并加入对照身份；计划变化必须重新运行免费检索对照再确认，不能用旧页面悄悄生成另一配置。默认参数也做类型规范化，隐式/显式相同设置复用同一记录。

## 网站管理入口与具体部署前置条件

新增 `create_app(settings, experiment_owner_verifier=...)` 接口，管理页路径 `/admin/algorithms/experiments`。主站没有现成的所有者登录、签名会话或可核验角色适配器，**本 PR 不补造账户/口令，不默认信任 Cloudflare、转发头或请求里的 owner 字段**。当前普通 `research-web` 不具备所需认证参数；仅设置开关而没接认证会直接报配置错误，不能公开启用。

部署前必须先完成并审核实际认证组件：校验签名、issuer/audience、有效期、所有者角色/允许名单及可撤销会话；只从已验证主体计算允许来源。不能用 `lambda: owner`、客户端头或代理 email 直接充当验证。适配器可同步或异步，返回 `ExperimentOwner(subject, session_id, frozenset(source_ids))`，未登录/非所有者返回 None。session_id 必须绑定实际会话，不得为所有访客共用常量。抛错、错误类型、空来源或缺失身份一律拒绝。

在上述认证已实现之后，由独立 ASGI 装配模块接入，例如：

```python
from researchlib.config import Settings
from researchlib.web.app import create_app
from your_verified_auth import verify_research_owner  # 需实际实现与审核，本仓库没有这个模块

app = create_app(Settings.from_env(), experiment_owner_verifier=verify_research_owner)
```

配置项（本次未修改任何线上值）：

| 设置 | 默认 | 含义 |
| --- | --- | --- |
| `RESEARCH_EXPERIMENTS_ENABLED` | 0 | 认证装配完成后才允许挂载管理页/API |
| `RESEARCH_EXPERIMENTS_SNAPSHOT` | 无 | 明确授权、已审核的本地私有原文快照 |
| `RESEARCH_EXPERIMENTS_DB` | 无 | 已初始化、权限 0600 的独立实验 SQLite |
| `RESEARCH_EXPERIMENTS_SITE_MODEL` | 0 | 允许界面明确调用现有站内模型；不会因开启而自动生成 |
| `RESEARCH_EXPERIMENTS_DAILY_PAIRS` | 5 | UTC 每日组数额度，1～20，失败和未知也计入 |

还需 HTTPS 入口、与 `PUBLIC_BASE_URL` 一致的 Host/Origin、主站正常运行配置及已授权的模型凭据。回环地址/隐藏路径/Cloudflare Tunnel 本身均不证明所有者身份。管理页、API、来源、下载和资源都逐请求验证；仅已认证且有快照全部来源权限的所有者看到管理链接。CSRF 绑定主体和会话；写请求必须同 Origin。来源配置保持启用且与快照一致，模型调用每侧前后，以及内置适配器取得并发名额后、凭据准备前与网络发送前重新检查来源和会话；检查发现撤销时拒绝发送，不回传部分原文/答案。评价和生成记录服务端主体用于审计。

网站管理入口使用同一批冻结原文，**不会自动读取生产资料生成快照或接受任意新问题**；新增题组仍需操作员在授权副本捕获并配置快照。主站默认搜索不切换候选。真实认证、网站开关、数据快照、模型凭据和部署均为尚未执行的运维前置条件；只有这些完成后，才可称网站管理功能已启用。

本机浏览器验收另可运行 `node tools/experiments/website_smoke.cjs`（同样指定 `EXPERIMENT_PYTHON` / `EXPERIMENT_CHROMIUM`）。它只启动回环测试服务，使用明确标记的合成认证/模型，以测试请求头模拟 HTTPS 上游；不代表真实身份认证或线上部署验收。


网站接入与内置模型适配完成后的本地验收：**1191 passed、0 failed、0 skipped**。新增网站与模型单元 33 项，覆盖默认关闭/缺认证拒绝启动、身份头伪造、来源权限、会话 CSRF/撤销、DeepSeek/Grok 双方案、计划变化、缺凭据/限流/超时/空答/截断、每日额度并发、模型每侧前后授权与预算，以及默认参数回放。两套浏览器验收及原 1500 篇检索基线均通过；全部为隔离数据库、合成会话/原文/模型替身，没有真实模型或线上认证验收。真实所有者认证仍是阻止网站启用的明确前置条件，默认开关未变。


同步当前 main `984c99583856c2914d9e48f711fd6a86010b366f`（#241 译制修复）后，完整本地回归 **1230 passed、0 failed、0 skipped**；无冲突同步仅用于草稿分支，不是把实验功能合入 main。上述 1191 为同步前结果。实验相关共 157 项；普通与网站两套浏览器、1500 篇检索基线均通过。当前仍缺真实所有者认证适配器，线上开关、数据与模型凭据未配置或核验，功能保持默认关闭。


## 并发队列中的授权撤销

独立复查在 `08fabcc` 复现：调用前检查通过后，线程在站内模型并发名额上等待；期间撤销会话或停用来源，旧代码在取得名额后仍会开始模型 POST，事后才返回 403。现已把检查回调传进内置适配器，在取得名额后先重新验证，再取/刷新凭据；凭据准备结束后、紧邻 `opener.open` 前再次验证。授权拒绝保留 403 和失败占位，不变成普通模型错误，也不自动重发。

等待期间撤销不会立刻唤醒排队线程；它在名额可用后发现撤销并拒绝发送，失败占位仍计入每日额度。**最后一次复查通过之后才发生的撤销，或已经进入网络发送流程的请求，无法保证取消或免于计费**；这里不宣称远端撤销与网络发送原子同步。已开始的认证刷新同样不能保证撤销，但其后检查发现权限失效时不会继续开始模型请求。自定义 `--generator` 内部若有自己的等待队列/网络调用，需要自行实现相同边界，通用自定义接口不保证中断其内部队列。

新增四项真实信号量并发反例覆盖 DeepSeek/Grok 各自的会话撤销和来源停用：旧实现四项均失败，修复后均在名额释放后返回 403，替身统计的凭据获取和模型 POST 都为 0。另覆盖凭据准备期间撤销的两项反例，以及缺少发送检查回调时拒绝执行；全部无真实网络、认证或模型请求。


并发发送边界修复后的完整本地回归：**1237 passed、0 failed、0 skipped**；网站/模型单元 40 项，含本次新增 7 项。网站浏览器验收再次通过，真实模型调用为 0。

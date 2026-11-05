# 本机算法实验台

这是站点所有者自己调参、用同一组问题比较两套结果的第一版。覆盖关键词搜索排序、问库选段与提示词、可选的两版回答及人工评价。不是随机流量实验，不会自动认定赢家或发布参数。

主站公开且没有现成的所有者身份认证，实验因此沿用独立本机工具的访问方式，**不在主站挂入口**。普通主站启动不创建实验库、不改变搜索配置、不增加迁移、不调用模型。首次实际使用仍需所有者提供明确授权的离线库副本；本 PR 仅发布实现供评审，不读取生产数据、创建生产副本、合并或部署。

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

问库实验使用较小的单次材料预算与前后文，不调用生产问库执行器、不写 `llm_answers`，不走自动扩词、deep/map-reduce、全篇/目录特殊处理、无证据概览回退或重试。所以 A 的含义是“当前词法评分 + 实验预算”，**不等于线上完整问库端到端基线**。不在这里修改 #225 的检索修复或 #239 的聊天页面。

每条对照保存：快照 hash、问题/范围、两侧完整参数、实现源码 hash、原文/段号/引用/提示词、时间与人工评价。实现 hash 覆盖评分、问库提示、实验引擎和参数 schema；更换这些实现会产生新对照 ID。相同问题/快照/参数/实现产生相同 ID，历史结果不覆盖。耗时只是本机回放参考，不代表生产延迟；首次计算结果含耗时，重复运行读取已冻结记录。

## 可选回答生成与费用

默认仅看原文及提示词，始终零模型调用。可从页面导出实际 messages，交给自选工具；这里没有默认外部模型地址或主站凭据接入。

若希望在页面生成，操作员需显式提供一个**自己审核的** `module:function`：

```bash
python -m researchlib.experiments serve \
  --snapshot data/experiments/private-v1.json --db data/experiments/private-v1.sqlite3 \
  --sources config/sources.yaml --generator your_adapter:generate
```

适配器同步接口：`generate(*, messages: list[dict], max_output_tokens: int) -> dict`。返回字段为 `text, model, model_version`，可选 `input_tokens, output_tokens, cost, currency`；金额只有有币种时才接受，未知费用/用量保留 null。必须自行实现输出上限、HTTP 超时及真实版本回报；适配器内部调用及重试由其实现负责。不要在适配器导入时自动调用模型。第一版未附真实付费适配器，也未验证任何真实模型质量。

网页必须勾选当前题的数据传递/费用确认后才调用。实验台每题仅各调用两侧适配器一次；自身无扩词、分批、自动重试。每侧传 `max_output_tokens=1500`，完整提示词超 40000 字符或任一侧无证据时拒绝调用。主站完整问库可能另有扩词、分批和重试，**不能把“两侧”当作实际 HTTP 次数或据此估造费用**。

SQLite 先原子占位并保存已尝试侧，再调用适配器；并发和重复请求复用占位。每侧成功立即保存，部分失败保留成功侧与未知费用，不自动重跑。进程中断后 running 表示结果可能未知，不会因刷新再次调用；需要操作员核实适配器/供应商记录。没有自动清理占位或付费重试按钮。只有两侧成功才允许回答人工评价。生成内容仅作文本展示，原文和回答里的 HTML 不执行。

## 人工评价与现有标注工具

搜索、问库选段和回答分别记评价。“相当”“都不好”“跳过”“无法判断”“撤销”独立计数；B 偏好比例只在明确 A/B 方向题内计算，无方向评价时为 null。改判使用修订号与请求 ID，重复提交幂等，过期页面 409，旧事件完整保留。不会把弱标当人工真值、把未评价当负例、声称统计显著或泛化提升。每次调参都会看过这组题，需要另建独立问题快照再验证，第一版不自动划训练/留出集。

已有的 [原文相关性盲测](relevance-annotations.md) 继续独立使用。本实验是带方案名字的整组结果人工评价，不伪称原文盲测、也不生成或混写它的训练标签。#99 的重排/训练闭环未合并且冲突，没有复刻或自动加载其学习权重。

## 本机访问和回退

服务固定监听 127.0.0.1，关闭代理头信任；真实 peer、Host、Origin、跨站和代理头检查覆盖读写及下载。写请求需要本机页面 CSRF，正文上限 64KiB，静态资源白名单，CSP 禁止脚本注入与被框架嵌入。它是本机信任边界，**不是远程所有者登录**；不要映射到 Cloudflare Tunnel 或公开代理。远程使用需另做认证和部署方案。

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

本轮本地结果（main 基点 `78dd8bedd8747fa49e8f7598148f10c45e86e1e8`）：完整单元＋集成 **1099 passed、0 failed、0 skipped**，其中新增实验单元 61 项、PostgreSQL 捕获集成 4 项。1500 篇／10463 块旧检索基线无退步；Ruff、JS 语法、diff 与锁定依赖检查通过。浏览器实际通过参数隔离、三个视图、评价连点/修订/撤销、原文安全显示、刷新恢复、键盘切换、显式无网络替身生成与导出，并核对 1440/1024/390 宽度无横向溢出。全部使用本机隔离 PostgreSQL、原创合成材料和模型替身，真实模型/向量调用为 0。

另在独立临时目录叠加 #225 的 `chunks.py`、`embeddings.py`、`retrieval.py` 源码补丁，能够干净应用；新功能 65 项测试及同一检索基线再次通过。这是针对性组合验证，不是 #225 的完整联合验收。#239 的问库聊天页面与本功能路由、模板、静态文件分离，仅检查了改动路径，未做 #235/#239 全分支组合测试。#99 保持独立未合并，现有 relevance SQLite/标签协议不修改。

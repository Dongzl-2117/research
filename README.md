# 投资研究库

给 **investment.zldong.com** 用的研究归档应用。代码公开，站点中的原文、图片、数据库和访问凭据不随代码公开。仓库名仍然是 `research`。

这里保存几位作者的全文归档。首页按天推送，也可以按作者、按公司分模块查看，并做中文全文检索。它不是新闻流水线。

## 运行

开发或接手项目先看下方[开发](#开发)，使用独立 clone 和临时测试库。不要在 mini 正在运行的 checkout 中切分支或直接套用启动命令。新空环境的服务初始化另见 [docs/deploy.md](docs/deploy.md)，不是测试的默认步骤。

浏览器只通过本机 `http://127.0.0.1:8088` 访问。公网入口是 Cloudflare Tunnel 转到这个地址，前面再加 Cloudflare Access。应用自己不提供登录。部署步骤见 [docs/deploy.md](docs/deploy.md)。

## 页面

- 按天：首页。只排 `timeline: true` 的来源（公众号、知识星球；飞书文库不进这一列）。文章按上海日历从新到旧，同一天里也是新的在前。翻页按整天翻，可以跳到某一天（`/?date=YYYY-MM-DD`）。点日期进入日汇总。卡片优先显示摘要；还没有摘要时用正文里的一句实质内容，不拿点赞、sn、时间戳当导读
- 按天的首页顶部有「最近观点」：按最近一条观点的时间排，不限持仓。收起时是一行公司名；展开后有导读的公司在下面显示库里已有的导读，没有导读就只留态度和近 30 天篇数，不把判断原文铺开。备份、定时任务、LLM 出了问题，或者定时导入的来源停更明显超过平时的间隔时，顶上提示一行；正常时什么都不显示
- 作者：每位作者的篇数、时间跨度、最近一篇、常深入谈的公司；作者页可以只在他的文章里检索
- 公司：按深入篇数排，可按市场筛、按最近提及排。页面上不标「持仓 / 观察」。公司页有四格统计、按月的深入 / 一带而过柱状图、「谁在谈」作者分布，文章按年份分组，默认只看深入提及，每篇显示提及原句。被深入谈到的公司都有「观点」时间线（不限持仓）：每篇一条作者判断和机制，原文谈到时带风险、推翻条件、验证指标，可以只看风险与反方、只看态度变化、只看某条论点。最近一条收成一行，展开是导读。观点够数（至少 4 条）的公司另有「论点」：把判断按机制归成几条，支持和反对各几篇、谁站哪边。打开公司页自动补论点默认关（`RESEARCH_THESES_AUTO`），夜间补论点也默认关（`THESES_ENABLED`）
- 检索：输入关键词或直接问一句话。所有词都命中的文章排前面、按相关度排序，每篇给最相关的原文段落；其后是只命中一部分的。图片里的字（OCR）也能搜到。可按作者、公司、日期过滤，检索词是公司或作者名时直达。怎么找、准不准、库大了快不快见 [docs/retrieval.md](docs/retrieval.md)
- 问库：先在库里召回最相关的原文段落，马上列出来；模型在后台补同义说法、再检索一遍，只依据原文整合，先写判断、再写作者给出的机制，数字只作证据，每句带出处。资料多时分批读完再汇总。写完逐句核对出处，数字、引号、作者、日期对不上的列出来。每段原文可以标「不相关」，也可以补「漏了」的文章，这些标注用来回放评测
- 文章：标题、时间和正文。从问库、观点的出处点进来，直接滚到被引用的那几段并标出来。底部「同期别人怎么看」列出别的作者前后 30 天对同一家持仓公司的判断。作者改过或删过的稿，署名行标出来，点进去看各版改了什么。英文稿可站内译成中英对照（见 [docs/translate.md](docs/translate.md)）。图片从本机磁盘读，外链图片也下载到本地。Markdown / JSON 仍在 `/api/v1`
- 重复文章：认出来的同一篇，每组留下哪份、藏起哪几份、重合多少，认错了点一下拆开

给 AI 用的 JSON 接口、MCP 和 `/llms.txt`，以及默认 DeepSeek、可选与 AIHOT 共用的 Grok 登录，见 [docs/llm.md](docs/llm.md)。

## 来源

白名单在 `config/sources.yaml`。现在启用方伟公众号、方伟知识星球、乘风破浪、林文丰飞书、孤独进化体、段永平博客、雪球「黑黑无常」。林文丰公众号停用，不入库。段永平是离线 PDF，有日期的进按天，没日期的只进作者、公司和检索。黑黑无常只导入已经抓下来的原创帖和长文，进首页按天。

持仓和观察名单在 `config/portfolio.yaml`，按公司表里的中文名写。页面上不展示这份名单，JSON 里的 `portfolio` 字段仍读它。观点、论点和首页「最近观点」不按它过滤。

新文章靠 Mac mini 上的定时抓取进来：方伟公众号和方伟星球每天各抓两次（`scripts/crawl-fangwei-*.sh`，launchd 安装见 [docs/backup.md](docs/backup.md)）。乘风破浪、孤独进化体、黑黑无常还没有定时抓取，要手动导入。AIHOT 已下线。

导入命令、幂等规则、改稿删稿怎么处理、去重、外链图片和计数报告见 [docs/ingest.md](docs/ingest.md)。方伟星球补真实钟点、林文丰目录树也在那里。公司提及的判定和歧义别名的规则见 [docs/mentions.md](docs/mentions.md)。备份、试恢复、夜间补齐、定时增量导入和出问题时的提示见 [docs/backup.md](docs/backup.md)。图片里的字见 [docs/ocr.md](docs/ocr.md)。

## 数据

Postgres 使用 zhparser 做中文全文检索。正文在库里，图片在 `data/images`。不要把 `.env`、归档压缩包或 `data/` 提交进仓库。

正文另外切成段落块存在 `article_chunks`，检索片段和问答引用落在块上；英文文章的中文译文也切块进检索。同一篇从两条路导入的，只留一个主版本。可选语义检索使用 pgvector；未配置服务或扩展时按关键词检索，见 [docs/retrieval.md](docs/retrieval.md)。

## 开发

### 先选对代码

2026-10-26 05:41 UTC 核验：`main` 为 `2d978ac`，包含 PR65 的 squash 合并及其后的 PR60 采集/健康检查修改；[PR56](https://github.com/Dongzl-2117/research/pull/56) 仍未合并，其已验证代码基线为 `2264b07173b920feed74c2fd3713355e7bd0d3d9`（tree `719a0520ad80150bc4f81724bd2b174337820e9a`）。后续文档提交不改变该代码验收口径。PR56 包含严格来源/缓存门槛及只读启动等修改，不能把两个分支的行为和测试成绩混用；每次工作前重新核对远端。

此时 PR56 与 main 有合并冲突，尚未做冲突整合验收。后续整合必须保留双方有效修改，重跑受影响检查；不能将某个分支整文件覆盖另一方，也不要把本文当作直接合并或部署指令。

在自己的开发目录克隆；以下不操作运行中的服务：

```bash
git clone https://github.com/Dongzl-2117/research.git research-dev
cd research-dev
git fetch origin pull/56/head:refs/remotes/origin/pr56
git switch -c work-pr56 origin/pr56
git rev-parse HEAD
git status --short
```

代码、锁文件、合成夹具及下述命令都在 GitHub；mini、本地 Linux 或自备 CI 都能使用，云环境只是可选执行场所，不需要某个 AI 的云任务、会话附件或临时报告。数据和运行配置另由本机维护者管理，不从生产目录复制到开发环境。

### 锁定依赖与普通单测

`pyproject.toml` 要求 Python >=3.12；建议与锁文件生成和 CI 一致使用 Python 3.12、普通非 root 用户。补丁版本和 CPU 架构以本机实际为准，不能把某一次 Linux 环境版本当唯一要求。

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pip check
python -m ruff check .
```

不要在此 clone 放生产 `.env`、OAuth 文件、原始归档或 `data/`。下面用清洁子进程环境运行单测，临时目录只存测试数据，不继承线上 API key 或模型端点：

```bash
bash <<'BASH'
set -euo pipefail
test ! -e .env || { echo '请使用不含 .env 的独立 clone' >&2; exit 1; }
test_tmp=$(mktemp -d "${TMPDIR:-/tmp}/research-unit.XXXXXX")
mkdir -p "$test_tmp/tmp" "$test_tmp/data"
env -i PATH="$PWD/.venv/bin:$PATH" TMPDIR="$test_tmp/tmp" \
  DATA_DIR="$test_tmp/data" XAI_OAUTH_TOKEN_PATH="$test_tmp/no-oauth.json" \
  DATABASE_URL=postgresql://research:test-only@127.0.0.1:1/research_test \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest tests/unit -q
BASH
```

普通单测使用 mock、合成文件和内存 SQLite，不需要生产数据或真实模型。个别安全负例会绑定回环端口；执行环境不允许时，只针对 `test_connection_to_a_private_peer_is_dropped` 明确选择 `-k 'not test_connection_to_a_private_peer_is_dropped'`，报告未运行数量，不能将跳过称为通过。不要改权限来追绿。

`pytest` 不带路径会同时发现集成测试。`tests/conftest.py` 的 `conn` fixture 会运行迁移/种子准备并 **TRUNCATE 多张表、重置 ID、提交事务**；库名守卫不是完整隔离。不得指向正式库或共享测试库，不设置 `ALLOW_TEST_DB=1` 绕过。一次性 PG/pgvector/zhparser 环境与固定回归命令见 [部署文档的测试环境](docs/deploy.md#一次性集成测试环境)。

依赖升级仍走现有 `scripts/lock.sh` 与 [依赖说明](docs/deploy.md#依赖锁死)，不为测试失败擅改锁。合成样本不放真实网友昵称、群名或私人原文；来源作者身份确为规则所需时，单独保留必要作者证据。

### 已有验证与未完成范围

| 代码/环境 | 实测范围 | 不能据此宣称 |
| --- | --- | --- |
| PR56 `2264b071`，mini + 隔离 Linux arm64 | 1051 单测通过、1 个回环测试未运行；固定原生 SQL 12 项通过，含 45 条别名对照；全仓 lint 通过；[CI #275](https://github.com/Dongzl-2117/research/actions/runs/40267531048) skipped | 完整 integration、真实模型召回/生成、生产性能或部署已验收 |
| 旧 `465441a`，Debian 13.6 x86_64 / Python 3.12.14 的补充冒烟 | dev lock 35 项匹配；83 项代表单测、10 个合成页面通过；PG16.15 / pgvector0.6.0 / zhparser2.4 只读运算通过；旧基线 6 个 lint 失败 | 当前 main 或 PR56 最终验收；以上组件版本不是新的唯一版本约束 |

生产部署仍需另行核验历史内容可用性、备份与恢复、运行入口和真实检索质量。严格 PR56 候选与保持旧展示资格的兼容保护方案分开，见 [部署门槛](docs/deploy.md#更新) 和 [历史内容计划](docs/availability-gate-2026-10-23.md)。不要把启动采集、翻译、模型、维护、回填或恢复作为接手后的默认步骤。

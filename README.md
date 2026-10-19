# 投资研究库

给 **investment.zldong.com** 用的私人研究库。仓库名仍然是 `research`。

这里保存几位作者的全文归档。首页按天推送，也可以按作者、按公司分模块查看，并做中文全文检索。它不是新闻流水线。

## 运行

```bash
cp .env.example .env
docker compose up -d --build
```

浏览器只通过本机 `http://127.0.0.1:8088` 访问。公网入口是 Cloudflare Tunnel 转到这个地址，前面再加 Cloudflare Access。应用自己不提供登录。部署步骤见 [docs/deploy.md](docs/deploy.md)。

## 页面

- 按天：首页。只排 `timeline: true` 的来源（公众号、知识星球；飞书文库不进这一列）。文章按上海日历从新到旧，同一天里也是新的在前。翻页按整天翻，可以跳到某一天（`/?date=YYYY-MM-DD`）。点日期进入日汇总。卡片优先显示摘要；还没有摘要时用正文里的一句实质内容，不拿点赞、sn、时间戳当导读
- 按天的首页顶部有「持仓近况」：持仓和观察的公司各自最近一篇深入谈到它的文章，和近 30 天的篇数
- 作者：每位作者的篇数、时间跨度、最近一篇、常深入谈的公司；作者页可以只在他的文章里检索
- 公司：持仓和观察的放在最前，每家一张卡，带深入篇数、近两年每月深入篇数、主要作者和最近一篇；其余公司按深入篇数排，可按市场筛、按最近提及排。公司页有四格统计、按月的深入 / 一带而过柱状图、「谁在谈」作者分布，文章按年份分组，默认只看深入提及，每篇显示提及原句
- 检索：输入关键词或直接问一句话。所有词都命中的文章排前面、按相关度排序，每篇给最相关的原文段落；其后是只命中一部分的。可按作者、公司、日期过滤，检索词是公司或作者名时直达。怎么找、准不准见 [docs/retrieval.md](docs/retrieval.md)
- 问库：先在库里召回最相关的原文段落，马上列出来；Grok 在后台补同义说法、再检索一遍，只依据原文整合，每句带出处。资料多时分批读完再汇总
- 文章：标题、时间和正文。英文稿可站内 Grok 译成中英对照（见 [docs/translate.md](docs/translate.md)）。图片从本机磁盘读。Markdown / JSON 仍在 `/api/v1`

给 AI 用的 JSON 接口、MCP 和 `/llms.txt`，以及与 AIHOT 共用的 Grok 登录，见 [docs/llm.md](docs/llm.md)。

## 来源

白名单在 `config/sources.yaml`。现在启用方伟公众号、方伟知识星球、乘风破浪、林文丰飞书、孤独进化体。林文丰公众号停用，不入库。

持仓和观察名单在 `config/portfolio.yaml`，按公司表里的中文名写。首页「持仓近况」、公司列表置顶、公司页的「持仓 / 观察」标记都读它，改完刷新页面即可。

导入命令、幂等规则和计数报告见 [docs/ingest.md](docs/ingest.md)。公司提及的判定见 [docs/mentions.md](docs/mentions.md)。

## 数据

Postgres 使用 zhparser 做中文全文检索。正文在库里，图片在 `data/images`。不要把 `.env`、归档压缩包或 `data/` 提交进仓库。

正文另外切成段落块存在 `article_chunks`，检索片段和问答引用落在块上。语义检索留了接口，但没有安装 pgvector，也没有向量列。以后可以给 `article_chunks` 配一张向量表，不必改 `articles`。

## 开发

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

集成测试需要带 zhparser 的 Postgres，并使用名为 `research_test` 的库。`docker compose up -d db` 之后创建这个库即可。CI 会自己构建数据库镜像并跑这些测试。

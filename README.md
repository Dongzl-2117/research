# 投资研究库

给 **investment.zldong.com** 用的私人研究库。仓库名仍然是 `research`。

这里只做两件事：按白名单保存几位作者的全文归档，以及按公司别名找出文章里怎么提到他们。它不是新闻流，也不是日报流水线。

## 运行

```bash
cp .env.example .env
docker compose up -d --build
```

浏览器只通过本机 `http://127.0.0.1:8088` 访问。公网入口是 Cloudflare Tunnel 转到这个地址，前面再加 Cloudflare Access。应用自己不提供登录。部署步骤见 [docs/deploy.md](docs/deploy.md)。

## 页面

- 归档：已启用来源的标签和文章列表
- 文章：原文 Markdown，图片从本机磁盘读
- 检索：跨来源全文搜索，给出作者和来源的命中篇数，并高亮片段
- 公司：代码、中文名、简称，列出提到它的文章、深度，以及按月篇数

## 来源

白名单在 `config/sources.yaml`。现在启用方伟公众号、方伟知识星球、乘风破浪。孤独进化体和林文丰飞书先占着位置。启用它们只改配置。

导入命令、幂等规则和计数报告见 [docs/ingest.md](docs/ingest.md)。公司提及的判定见 [docs/mentions.md](docs/mentions.md)。

## 数据

Postgres 使用 zhparser 做中文全文检索。正文在库里，图片在 `data/images`。不要把 `.env`、归档压缩包或 `data/` 提交进仓库。

语义检索留了接口，但没有安装 pgvector，也没有向量列。以后可以加一张 `article_embeddings` 表，不必改 `articles`。

## 开发

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

集成测试需要带 zhparser 的 Postgres，并使用名为 `research_test` 的库。`docker compose up -d db` 之后创建这个库即可。CI 会自己构建数据库镜像并跑这些测试。

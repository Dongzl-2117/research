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

- 按天：首页。文章按上海日历从新到旧，同一天里也是新的在前。翻页按整天翻
- 作者：按作者查看他的文章，同样按天排列
- 公司：按公司查看提到它的文章
- 检索：在标题、作者、正文里做中文全文检索
- 文章：原文 Markdown，图片从本机磁盘读

## 来源

白名单在 `config/sources.yaml`。现在启用方伟公众号、方伟知识星球、乘风破浪、林文丰飞书。孤独进化体和林文丰公众号停用，公众号不入库。

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

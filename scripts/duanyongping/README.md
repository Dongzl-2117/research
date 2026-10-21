# 段永平博客 PDF

来源 id：`duanyongping-blog`，页面上的名字是「段永平」。和别的已启用来源一样走白名单；抽出来的 Markdown 用现有的 Markdown 导入器入库。

只收论证落在具体公司上的条目。生活感悟、只讲方法、公司名只出现在列举/致谢/闲聊里的，跳过。「回复段永平」的博友帖不当他本人的帖。问答整条保留：读者的问题、段永平的回答，以及回答后面别人的回复。段永平那节只剩「回本文开头」或署名加时间、没有正文的，整篇不留。有日期的进首页按天；没有日期的不进按天，作者页、公司页和检索仍能找到。观点和摘要只把段永平本人的章节或「段：」段落交给模型。`file:///H.HTM` 一类页码垃圾在抽文字时去掉。

PDF 不在仓库里（约 40MB，约 4440 页）。预期路径：

```text
/Users/dongzhilin/Downloads/投资资料/段永平博客文章合集（+2006+-+2018+）.pdf
```

## 入库

把 PDF 拷到 `data/inbox`（这个目录不进 git），文件名保持不变，然后一条命令：

```bash
docker compose exec app research-ingest-duanyongping \
  --pdf "/data/inbox/段永平博客文章合集（+2006+-+2018+）.pdf"
```

容器里要有 poppler（镜像已装 `pdftohtml`、`pdftotext`、`pdfinfo`）。优先用 `pdftohtml -xml`，加粗的段落当成段永平的回答；抽不出 XML 时退回 `pdftotext -layout`，靠「段永平：」这类署名。

只生成 Markdown 和分类报告、不写数据库：

```bash
docker compose exec app research-ingest-duanyongping \
  --pdf "/data/inbox/段永平博客文章合集（+2006+-+2018+）.pdf" \
  --emit-only
```

Markdown 写到 `$DATA_DIR/inbox/duanyongping-blog/`（容器里是 `/data/inbox/duanyongping-blog`），分类报告是同目录的 `report.json`。

本机已经装了 poppler，并且 `DATABASE_URL` 指向本库时：

```bash
research-ingest-duanyongping \
  --pdf "/Users/dongzhilin/Downloads/投资资料/段永平博客文章合集（+2006+-+2018+）.pdf"
```

先看前几页拆得对不对：

```bash
research-ingest-duanyongping \
  --pdf "/data/inbox/段永平博客文章合集（+2006+-+2018+）.pdf" \
  --pages 1-20 \
  --emit-only
```

同一篇再跑一遍不会复制。外部 id 由问题正文和段永平的回答算出来，不靠页码。

## 存量回扫

已经进库的旧数据不用重读 PDF，也不要在云端对生产库跑全量观点或 OCR。先回扫这一来源（不删文章，不调用模型）：

```bash
docker compose exec app research-ingest-duanyongping --reclassify --dry-run
docker compose exec app research-ingest-duanyongping --reclassify
```

会洗掉 `file:///H.HTM` 页码垃圾并重算检索文字和段落块；博友回复标成非本人（`poster_role=guest`），不进作者页和观点；没有具体公司，或公司只是被列举、致谢、闲聊点到的，标 `off_views` 并摘掉公司挂靠。段永平那节只剩「回本文开头」的，标 `suppressed=empty_answer`，不进按天、作者列表和检索。应用启动时也会做这一步，不必等回扫。

然后再只抽这个来源的观点：

```bash
docker compose exec app research-views --source duanyongping-blog --limit 100
```

本机 `DATABASE_URL` 指向本库时，把 `docker compose exec app` 去掉即可。全量 OCR 是另一条线，不在这里。

## 夹具

`fixtures/sample.xml` 和 `fixtures/sample.txt` 是几页假文：公司问答、只讲方法、只在「比如」里点到公司、有日期、没日期、转载。不需要那份 40MB 的 PDF。

```bash
python scripts/duanyongping/cli.py \
  --xml scripts/duanyongping/fixtures/sample.xml \
  --emit-only \
  --out /tmp/duanyongping-fixture
```

公司名单用研究库里已有的公司，另外补了他博客里常见、库里还没有的名字（万科、雅虎、步步高等）。补名单只决定收不收这篇，不改公司表。漏了的公司可以加在 `src/researchlib/ingest/duanyongping/companies.py` 的 `EXTRAS`。

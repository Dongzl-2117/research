# 图片里的字（OCR）

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。现在把本地图片（归档里带的、从外链下载来的，见 [ingest.md](ingest.md)）交给 RapidOCR 识别，识别出的字切成段落块进检索。

- 按图片内容哈希只识别一次，结果存在 `image_texts`。多篇引用同一张图不重复识别；识别不出字的也记一行，不反复去认。
- 置信度低于 0.5 的文字框不要（多半是花纹、二维码认成了字）。文字框按阅读顺序拼：从上到下分行，同一行从左到右。
- 识别出的汉字、字母、数字够 20 个的图才算有字，切成段落块：`article_chunks.lang = 'ocr'`，块号从 20000 起，标题「图片里的字」，锚点指向文章页上那张图（`img-<哈希前 12 位>`）。
- 检索、问库命中这种块时，结果里显示识别出的字；点进文章页，上面写「图片里的字 · 看这张图」，点一下跳到那张图。
- 公司提及不看图片里的字，免得水印、表情包里的字乱记。
- 重复文章（见 [ingest.md](ingest.md)）里的图不认。

## 单独的镜像

RapidOCR 要 onnxruntime 和 OpenCV，装上以后镜像大很多，所以不进主镜像，另有一个 `ocr` 镜像（Dockerfile 里的 `ocr` 阶段，在应用镜像上加 RapidOCR，依赖按 `requirements-ocr.lock` 锁死）。模型文件在安装包里，跑的时候不联网下载。只用 CPU。

```bash
docker compose --profile ocr build ocr
docker compose --profile ocr run --rm ocr research-ocr --limit 500
docker compose --profile ocr run --rm ocr research-ocr --limit 5000 --max-minutes 120
```

主镜像里跑 `research-ocr` 会直接退出（退出码 2），提示改用 `ocr` 镜像。

## 每晚补一批

夜间任务（[backup.md](backup.md)）默认不跑 OCR。镜像建好以后：

```bash
NIGHTLY_OCR=1 BACKUP_DIR=… scripts/install-launchd.sh
```

之后每晚在观点、摘要、论点之后再识别一批（`OCR_LIMIT`，默认 400 张，和其他步骤共用 `MINUTES` 的时间上限），失败了和其他步骤一样记进夜间任务的结果，首页提示。

第一次可以先手动跑一大批把存量认完，以后每晚只剩新导入的图。

## CI

CI 会构建 `ocr` 镜像，用 `tests/fixtures/ocr-sample.png`（三行中文）跑一遍，认不出「拼多多」「广告收入」「货币化率」或行的顺序不对就失败。锁文件里的版本升级以后，靠这一步确认还能用。

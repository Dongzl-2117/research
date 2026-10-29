# 图片里的字（OCR）

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。现在默认把本地图片交给站内 deepseek-flash 识图（`llm.chat`，多模态 content），识别出的字切成段落块进检索。RapidOCR 只作显式回退，不进主路径。

- 按图片内容哈希只识别一次，结果存在 `image_texts`，`engine` 默认 `deepseek-flash-vision`。多篇引用同一张图不重复识别；识别不出字的也记一行，不反复去认。识图失败（无 key 在开工前退出；单张 HTTP 错）记 `engine` 以 `:error` 结尾、正文为空，下次不带 `--redo` 也会重试，不把失败改写成 RapidOCR 的脏字。
- 识图按气泡从上到下转写：绿色气泡写成「方伟：」，其他人一律「网友：」，不留真实昵称。看不清写 `?`，禁止编造。表格和柱状图要求公司与数字一一对应。提示词在 `researchlib/ocr.py` 的 `VISION_PROMPT`。输出若已经是「方伟：/网友：」，阅读页和旁路仍再剥界面字，不把 UI 行标成说话人。
- RapidOCR 置信度低于 0.5 的文字框不要（多半是花纹、二维码认成了字）。文字框按阅读顺序拼：从上到下分行，同一行从左到右。有原图时按微信绿气泡底色标本人，没有图时退回「行左缘靠右」。
- 识别出的汉字、字母、数字够 20 个的图才切检索块：`article_chunks.lang = 'ocr'`，块号从 20000 起，标题「图片里的字」，锚点是 `img-<哈希前 12 位>`。检索块先走和旁路同一套整理，界面字不进检索。检索和问库走这些块，不另建索引。
- 阅读、摘要、观点放宽到 8 个字。空白、水印、几个字的装饰不算。刚过线的拿不准，算有用，不能漏。不按「是不是微信、是不是表格」分：有可读的字就用。
- 公司提及不看图片里的字，免得水印、表情包里的字乱记。这条不变。
- 识别结果另外写在文章旁路 `articles.ocr_text`。微信群聊截图先整理成「方伟：」「网友：」再写入：绿色气泡是作者自己，标成方伟；灰色、带别人昵称的标成网友，不留本名（含「李小红@think」、Kevin）。新识别的靠右行会带侧标记。没有坐标的旧结果里，不带别人昵称的气泡也当绿色本人，不再因为没写「方伟」就标成网友。显示名「方伟-最近在杭州」仍是方伟。句子里的 @方伟 不是说话人。方伟连续说的合成一块，网友每次开口仍分开。跨图重复的句子只留一次。群名、价格（含粘在商品上的「￥19.?」）、小程序及其误识「小小程序」、美团外卖、店名和商品卡、「设为群待办」、状态栏时间（含「10:38≥」）这些界面字去掉。知识星球的引用回复不是这套绿/灰气泡：第一条「方伟看十年」或「方伟看十年作者」账号栏上面的灰字是「网友：」，栏下的回复是「方伟：」，账号栏和下一帖标题去掉；栏上没有正文时仍整段是方伟。表格和贴成长文的图仍是原文。摘要和观点交给模型的正文接上这份整理后的字，和正文一视同仁。有效字少于 8 个的图不进这条旁路。打开文章时按已识别的字重写这一篇的旁路（不重新认图，也不扫全库）。观点和摘要再读这篇时，只要还挂着 `image_texts`，也会按当前规则从原始识别重写，避免库里已经写成「方伟：」的旧旁路把引用回复分不回来。
- 正文不足 400 字时，图片文字和上次抽的时候不一致，摘要会重排；打开这篇文章也会只排这一篇，不扫全库。已经写出判断的长文不会因为后补了一张图就整篇重抽。
- 识图写回 `ocr_hash` 之后（`research-ocr` 或 `POST /api/v1/ocr`），短正文会记进 `article_followups`：摘要一篇、方伟星球再加标题一篇。不看标题现在像不像壳——「没有可读文字」「本文正文为空」这类空壳以前不会被 `title_needs_ai` 认出来。同一次命令里最多接着重写 30 篇；剩下的留在队里。摘要队列把入队的、以及短正文哈希对不上的，排在「还没有摘要」的积压前面。标题回写（`--ai-titles`）也先处理入队的。完好的标题（例如「关于携程与Booking」）只出队，不打模型。长文不因为补了一张图就重排。应用启动不跑这一步。
- 观点队列：深入提及照旧。正文不足 400 字、图片里又有字的，一带而过也抽——判断经常在截图里，不能因为深度是一带而过、或内容来自截图就跳过。长文里顺带一提的公司，不会只因为旁边有张图就抽。夜间任务仍按原来的篇数上限，深入提及排在前面；全库重跑不由这次改动自动开始。单篇可以用 `research-views --articles <id>`。公司不在公司表里就没有观点可抽。
- 重复文章（见 [ingest.md](ingest.md)）里的图不认。

## 阅读页

文章页和飞书目录里的正文现查 `image_texts`，用和旁路同一套整理（`wechat_dialogue`）。整理完再过一遍识图校对（`vision_polish.repair_vision_misreads`，展示层 `polish_display_text` 同一条）：家电品牌并列里的「墓地」显示成「美的」（「格力和墓地」），单独的「去墓地扫墓」不动；`free cash flow` 中间只错一个字母（`free case flow`）显示成 free cash flow。不进 `fix_obvious_typos` 的语病表，也不改 `image_texts` 里的原文。新识图的 `VISION_PROMPT` 要求模型转写时就按这个判断写。文章 24008 的雪球截图、8880 的 free case flow 部署后打开页面即可核对，不必重跑识图。视频角标（未成年人请勿饮酒、主播名牌、以「观察/视界/频道」结尾的节目水印）和品牌广告海报的 Logo、四字对仗口号同样在阅读页剥掉；新闻标题、保障条款留下，书籍目录不删。24010、23173 部署后打开即可核对，不必重跑识图。群聊显示成「方伟：」「网友：」，连续几张微信截图合成一段，重复的句子不重复出现。雪球、微博嵌套回复按时间从早到晚排（上面的回复在后，下面的引用卡片在前）；分段跟识图写的空行走，只把半句折行接上，不把整张图收成一段，也不按句拆段。转写出来的字本身就是原图链接，点这段字打开图片；锚点仍在这个链接上。没有可读文字的商品图、图表仍是一行「原图 1」「原图 2」。文章 24008 的方洪波金句、雪球嵌套和原图链接，部署后打开页面即可核对，不必改库、不必等识图重跑；新的 `VISION_PROMPT` 让之后的识图直接按自然段和时间顺序写。字太少或还没识别的图保持现在的图片。整理完不够读就仍用识别原文，不编对话。新跑完的图刷新页面就能看见，不用重导正文。检索里的「看这张图」仍跳到 `img-<哈希前 12 位>`，落在对应的原图链接上。检索块和旁路用同一套整理，界面字不进检索。

## 站内识图（默认）

主镜像和 `app` 容器就能跑，不需要 onnx / RapidOCR。`research-ocr` 默认 `--engine vision`，调用站内 `deepseek-flash`（和问库、摘要同一模型）。送识图前会把图压到约 110KB（Pillow，边长≤1280），避免 DeepSeek 转发链路掐断大 data-url；瞬时断连会重试最多 3 次。识图请求的 max_tokens 固定 32768（覆盖站点默认下限；8k/16k 在 thinking 下会把正文挤空），避免流式思考把容器 OOM。也可用站内 API，和译制一样只接受本站或本机 POST：

```bash
# 定点重认。立刻返回 running，后台识图。
curl -s -X POST http://127.0.0.1:8088/api/v1/ocr \
  -H 'Content-Type: application/json' \
  -d '{"articles":[24002,24007],"redo":true,"limit":50,"engine":"vision"}'
curl -s http://127.0.0.1:8088/api/v1/ocr
curl -s -X POST http://127.0.0.1:8088/api/v1/ocr/stop

# 容器里同一条通路
docker compose exec -T app research-ocr --engine vision --redo --limit 50 \
  --article 24002 --article 24007
```

`engine`：`vision`（默认）、`rapidocr`（显式回退）、`auto`（有 LLM 凭证用 vision，否则 RapidOCR）。已经在跑时再 POST 不会开第二个。GET 看进度，不回 API key。单张失败记 `:error` 和空正文，下次会重试。

## RapidOCR 回退

RapidOCR 要 onnxruntime 和 OpenCV，不进主镜像。只有显式指定才走这条，日更不走。

```bash
docker compose --profile ocr build ocr
docker compose --profile ocr run --rm ocr research-ocr --engine rapidocr --limit 500
docker compose --profile ocr run --rm ocr research-ocr --engine rapidocr --limit 5000 --max-minutes 120
# 存量已经在 image_texts 里的图，要重认时加 --redo（全库约 2 万张，部署后在 Mac mini 上跑，不要在云端对生产库跑）
docker compose --profile ocr run --rm ocr research-ocr --engine rapidocr --redo --limit 5000 --max-minutes 120
# 只认某几篇里的图
docker compose --profile ocr run --rm ocr research-ocr --engine rapidocr \
  --article 7711 --article 7865 --article 7638 --article 7832 --article 651
```

没装 RapidOCR 时 `--engine rapidocr` 退出码 2。没配 `DEEPSEEK_API_KEY` 时默认的 vision 也是退出码 2，不会悄悄改走 RapidOCR。

OCR 写完后，图片帖的空白观点会因为 `ocr_hash` 对不上而进入补抽。确认「图片里的字进了观点输入」可以看这几篇（约 33 张图）：

```bash
docker compose exec app research-views --gap --articles 7711,7865,7638,7832,651
```

抽完后这几篇若原文在图里写了判断，`company_views.judgment` 不应再是空的，原句应能对上 `articles.ocr_text`。

说话人规则改了以后，已经写成非空判断的文章不会因为 `ocr_hash` 变化就自动重抽（正文短于 400 字、或判断仍是空白的才会）。文章 8881 的拼多多截图把栏上「不太看好」标成了方伟，要按这一篇重抽它提到的公司，不要全库、也不要加 `--gap`（`--gap` 会跳过已经有非空判断的）：

```bash
docker compose exec app research-views --articles 8881
```

阅读页本身现解析 `image_texts`，部署后打开即是「网友：」在账号栏上、「方伟：」在栏下，不必重跑 RapidOCR。上面这条命令会先按原始识别重写 `articles.ocr_text`，再重抽该篇的 `company_views`（拼多多，以及同一篇里从这张图写出的其他公司）。

知识星球和公众号截图上的页脚按行丢掉，不从第一行按钮截到图末。单独成行的按钮、粘在一行里的「分享21 推荐0 划线人数0」「赞赏￥0」「3条留言V 写留言」，以及公众号的「微信小店」「关注同城」「相关的服务号」，都不进「方伟：」。这些行后面的方伟、网友继续留着。「李四留言」「李四留言1次·关注8个月」，以及名字后粘着拉丁字母的「孙小平SunPing留言1次·关注3个月」「UI留言17次·新的关注」，改成网友的一轮（就算这一行已经写成「方伟：…留言头」）。「22:07上海」这种钟点加地名删掉。公众号再删「+关注」「14个朋友关注 3 19。」，账号名和按钮粘在一行的「饮料快暴 +关注」「品饮汇 品饮汇观察 +关注」，以及按钮后粘着阅读量的「观察 56个朋友关注 82 455 38 33。」也整行删；正文里提到「朋友关注」的句子不动。知识星球回复头的「方伟在学习（作者）2」是说话人分界，下面是方伟；「方伟看十年（作者）」仍是引用回复的账号栏。微信小店、拼多多、淘宝的商品卡截图不套对话，阅读页只留原图链接；正文句子里的「微信小店」照常保留。阅读页现解析，不必重跑 RapidOCR。部署后再跑一次回写，12681、473、606 的旁路会按新规则重洗（见 [ingest.md](ingest.md)）。`research-repair-zsxq` 同时重洗 `fangwei-zsxq` 和 `fangwei-mp`：

```bash
docker compose exec app research-repair-zsxq --dry-run
docker compose exec app research-repair-zsxq
docker compose exec app research-views --articles 203,227,473,537,606
```

不要加 `--gap`，也不要全库。8881 的拼多多已经抽过。抽验锚点是 12681、473、606。

## 每晚补一批

夜间任务（[backup.md](backup.md)）默认不跑识图。要每晚补新图：

```bash
NIGHTLY_OCR=1 BACKUP_DIR=… scripts/install-launchd.sh
```

之后每晚在观点、摘要、论点之后，于 `app` 容器里跑 `research-ocr --engine vision`（`OCR_LIMIT`，默认 400 张，和其他步骤共用 `MINUTES` 的时间上限）。不再 `docker compose --profile ocr run`。失败了和其他步骤一样记进夜间任务的结果。

`app` 容器没在跑时，这一步会先 `docker compose up -d app` 再执行，避免 `service app is not running` 直接失败。新导入的带图文章（方伟星球、方伟公众号，以及其他来源）在导入命令结束时就会排进识图，一次最多 40 篇，不必等到 00:37。夜间任务仍补没排上的。设 `RESEARCH_INGEST_FOLLOW=0` 可以关掉导入后的自动识图和译制。

第一次可以先用上面的 API 或 `research-ocr --article` 定点重认抽验锚点，不要无差别全库 vision。以后每晚只剩新导入的图。

短正文的 `ocr_hash` 若在这一步变了，命令结束前会用同一套站内 deepseek-flash 重写最多 30 篇摘要，方伟星球的空壳标题也一起重写；不是全库，长文不重排。没写完的留在 `article_followups`，下次摘要优先拿。

## CI

CI 会构建 `ocr` 镜像，用 `tests/fixtures/ocr-sample.png`（三行中文）直接调 `rapid_engine()` 跑一遍，认不出「拼多多」「广告收入」「货币化率」或行的顺序不对就失败。这只验收 RapidOCR 回退，不调用 DeepSeek。锁文件里的版本升级以后，靠这一步确认还能用。

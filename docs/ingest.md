# 导入

导入只接受 `config/sources.yaml` 里 `enabled: true` 的来源。停用的来源、不认识的 id、用错类型的命令都会直接拒绝，退出码是 2，数据库不会写。

以后要启用占位来源，把对应项的 `enabled` 改成 `true` 即可。类型已经配好：

| id | 类型 | 命令 |
| --- | --- | --- |
| fangwei-mp | wechat_mp | `research-ingest-wechat` |
| fangwei-zsxq | zsxq | `research-ingest-zsxq` |
| chengfengpolang | zsxq | `research-ingest-zsxq` |
| gudujinhuati | markdown | `research-ingest-markdown`；定时抓取 `research-crawl-wechat` |
| linwenfeng-mp | wechat_mp | `research-ingest-wechat` |
| linwenfeng-feishu | feishu | `research-ingest-feishu` |
| heiheiwuchang | markdown | `research-ingest-markdown` |

`linwenfeng-feishu` 保持启用，用 `research-ingest-feishu` 导入，但 `timeline: false`，不进首页按天和日汇总。作者、公司、检索、问库仍能找到它。`linwenfeng-mp` 停用，公众号不入库。雪球「黑黑无常」（`heiheiwuchang`）启用，`timeline: true`，进首页按天。只导入已经抓下来的原创帖和长文，不新抓；转发和评论回复不收。见下面的雪球专节。

段永平博客（`duanyongping-blog`，离线 PDF 一次性导入）10-30 下线：配置项、导入命令和只为它写的规则都删了。库里的文章、提及和观点没删，来源停用后网站、接口和问库都不再展示。

幂等键是 `(来源, 外部 id)`。外部 id 优先用 frontmatter 或知识星球 `topic_id`，否则用归档内的相对路径。同一篇再跑一遍不会复制；正文、标题或时间变了才更新。

## 改稿和删稿不覆盖

库里这份常常是唯一的一份：作者删了原文，或者导出工具抓到的已经是删除提示，重导一次就会把完整的全文换掉。所以重新导入时正文变了：

- 新正文像删除提示（「该内容已被发布者删除」「此内容因违规无法查看」这类，而且很短），或者比库里短一半以上：**不替换**，库里留完整原文和它的图片，只把这一版记进 `article_revisions`（`held`）。同一版以后再导入也不再记。导入报告里算「拦下」。
- 其余的改动照常替换，替换前把旧版记下来（`previous`）。只改了发布时间的不记；去掉 frontmatter、归档头和评论回复、不看空白以后正文一样的（星球归档头里的点赞、阅读数变了）也不记。以前这样记下的旧版，文章页不再算「改过」，改动页也不列。这种只动了归档头、标题或发布时间的更新，观点、摘要和译文的哈希跟着对齐，不重抽，摘要也不会从卡片上消失；评论回复变了的照旧重抽。
- 知识星球正文后面的「评论里的回复」（见下面）只是多了回复，照常更新，不算改稿；回复变少了（这次导出没带评论，或者回复被删了），库里那份更全，不动。判断删稿、截短只看作者写的正文。
- 知识星球话题会记下发帖人（`raw_meta.poster_name` / `poster_id` / `poster_role`）。名字和来源作者一致，或是来源配置里 `poster_names` 列的昵称（方伟在星球里叫「方伟Rey」），是星主原创（`owner`）；对不上的是网友帖（`guest`）。网友帖正文留在库里，网站上（首页、按天、检索、正文、接口、问库）都不展示，观点、摘要、识图也不处理它们。没有发帖人名字的旧数据先仍按星主，避免误删。星主改了昵称，把新昵称加进 `poster_names`：应用下次启动时按新名单重算已入库帖子的标记，不用重新导入；变成网友帖的，已有观点删掉。

文章页署名行标出「改过」「后来被删」「后来被截短」，点进去（`/articles/{id}/revisions`）看各版之间改了什么：只列改动的段落和前后各一段，中间没改的收成一行「略去 N 段没变的」。

## 同一篇只留一份

幂等键管不住两条路进来的同一篇：以前从 AIHOT 导入的外部 id 是 `aihot:<原 id>`，文件归档用路径或 `topic_id`，同一篇两边各导一次就是两行；同一位作者在公众号和知识星球之间转发也是。所以每篇写入后还会认一次重复：

- 正文按句切开，只留汉字、字母和数字，每句算一个哈希，每篇存哈希最小的 32 句当指纹（`article_fingerprints`）。中文按句末标点和换行切；英文（汉字不到字母的十分之一）把换行当空格、按句号切：同一篇从网站和邮件两条路进来，网站版说话人名字单占一行、句子中间还断行，邮件版不断，按行切一句也对不上（Stratechery 22029 和 22210）。
- 只和同一位作者、发布时间前后一周内的文章比。指纹重合够多的，再逐句核对：较短那篇至少 80% 的句子（且至少 3 句）出现在另一篇里，两篇长度也差不多，才算同一篇。同一位作者很多篇都有的句子（页脚、免责声明）不算。
- 主版本：图片多的；明显更全的（另一份不到它的四分之三，比如半截稿）；发得早的；先入库的。只差一行归档头的两份，先入库的那份留着。
- 知识星球同一来源、同一话题号（`raw_meta.topic_id`）的几份直接算同一篇，不比指纹。10 月 20 日以前导入的帖子没读到 `raw/` 里的话题 JSON，外部 id 是文件名，后来按话题号又导入一遍；新的一份带着评论回复、点赞数也变了，指纹常常对不上。话题号缺的从正文开头的归档头（`- topic_id：…`）补，迁移 `021_zsxq_topic_ids.sql` 给已入库的补上。同一话题里外部 id 就是话题号的那份当主版本，以后导入更新的是它；和别的来源（比如公众号全文）照常比长短。
- 其余的 `duplicate_of` 指向主版本，段落块、公司提及和观点删掉，不进首页、作者页、公司页、检索和问库。文章页还能打开，署名行写「同一篇也在 某来源」。

导入报告多一行「重复」。第一次部署时，应用启动后在后台把全库认一遍，日志里有开始和完成两行；以后只认新导入或改过的。认法升版本（`duplicates.VERSION`）时也全库认一遍，指纹全部重算。

认出来的都列在「重复文章」页（`/duplicates`）：每组一个留下的主版本和藏起来的几份，带句子重合比例、长度比例和什么时候认出来的。文章页署名行「同一篇也在」后面的「核对」直接打开这一组。认错了点「不是同一篇」，这篇恢复段落块和公司提及，以后不再参与认重复。命令行也可以：

```bash
docker compose exec app research-dedupe --separate 123   # 把这篇标成独立的一篇，以后不再参与认重复
docker compose exec app research-dedupe                  # 全库重认一遍
```

在 6000 篇的评测语料（大量套话句子反复出现，比真实文章重复得多）上，全库认一遍要一分多钟，误认 2 篇。

发布日期按这个顺序取：Markdown frontmatter（`date`、`publish_time`、`create_time` 等）、知识星球 raw JSON 的 `create_time`（包括嵌套的 `resp_data.topic`）、正文里的公众号发布时间标记、文件名里的日期。已入库但 `published_at` 仍为空、而正文或 `raw_meta` 里认得出日期的，可以补一次：

```bash
research-backfill-dates
```

这条命令只填空日期，不改已经有日期的行。找不到日期的仍留空，首页放在「日期不详」。

公司别名改完后跑：

```bash
research-rebuild-mentions
```

手工改过的公司行如果还想保留，先 `UPDATE companies SET seeded = false WHERE name_zh = '...'`。`seeded = true` 的行会在进程启动时被种子数据覆盖。

## 在运行中的 compose 里导入

把归档放到 `./data/inbox`（这个目录被 git 忽略）：

```bash
docker compose exec app research-ingest-wechat \
  --source fangwei-mp \
  --input /data/inbox/fangwei-mp

docker compose exec app research-ingest-zsxq \
  --source fangwei-zsxq \
  --input /data/inbox/fangwei-zsxq

docker compose exec app research-ingest-zsxq \
  --source chengfengpolang \
  --input /data/inbox/chengfengpolang

docker compose exec app research-ingest-markdown \
  --source heiheiwuchang \
  --input /data/inbox/heiheiwuchang
```

黑黑无常要先把抓取目录转成带 frontmatter 的 Markdown，再跑上面最后一条。步骤在下面的雪球专节。

也可以在宿主机上装好依赖，把 `DATABASE_URL` 指到 `127.0.0.1:5432` 后直接跑同样的命令。

## 微信公众号

接受一个目录或一个 zip。目录里可以同时有解压后的 Markdown 和 `01-text.zip`、`02-images-a.zip` 这种图片包。解析会：

- 跳过路径里带 `..` 的 zip 条目
- 再展开一层嵌套 zip
- 先按 Markdown 里的相对路径找图，找不到再按文件名在图片包里找
- 外链图片导入时不下载，之后另外下载（见下面「外链图片」）

正文用 UTF-8，失败时再试 GB18030。

方伟公众号 11 月 7 日以前的抓取脚本（`data/inbox/fangwei-mp/dl.py`，不在仓库里，现在不再调用）遇到极致了把换行写成字面上反斜杠 n 的那类正文时原样写进归档，整篇挤成一行。导入时一行里有挨着汉字或中文标点的 `\n`，这一行的 `\n`、`\r\n` 都换回换行；英文和代码里的不动（所有 Markdown 归档都这样处理）。`scripts/crawl-fangwei-mp.sh` 每次都把整个归档目录重新导入，已入库的那几篇下一次抓取时就改过来。

## 公众号定时抓取

`research-crawl-wechat` 抓配置里写了 `ghid`（公众号原始 ID，gh_ 开头）的公众号：孤独进化体（`scripts/crawl-gudujinhuati.sh`，每天 09:20）和方伟公众号（`scripts/crawl-fangwei-mp.sh`，每天 08:10、20:10；11 月 7 日起，以前用仓库外的 `dl.py` 取极致了按段估图位置的正文）。都由 launchd 定时跑，见 [backup.md](backup.md)：

```bash
docker compose exec app research-crawl-wechat --source gudujinhuati --dry-run   # 只看哪些是新的
docker compose exec app research-crawl-wechat --source gudujinhuati             # 抓新文章、写归档、导入
```

- 文章列表向极致了（dajiala）查：每查一次约 0.14 元，`--dry-run` 也要查。key 是仓库根 `.env` 里的 `DAJIALA_KEY`，docker compose 透传进容器。极致了限速每分钟 5 次，两次调用之间隔 13 秒。
- 列表里有比库里最新一篇还早的文章，却一篇都对不上库里的：多半是 `ghid` 写成了别的号，这次一篇都不取，退出码 1。列表里都比库里新的（很久没抓、新文章超过一页）照常取。
- 库里已有的跳过：按正文开头归档头里的 sn、原链接里的 `sn=`，或同一天同标题。AIHOT 和以前的归档导进来的都认得出。
- 新文章的正文先从公众号原文页（`#js_content`）取，不花钱，图片位置也是对的，归档头多写一行「- 图片位置：原文页」（导入时记成 `raw_meta.images = "page"`，阅读页不按识图挪图）；原文页要验证、图文消息这种没有正文的，再用极致了的正文（每篇约 0.03 元，图片位置是按段估的，不写这一行）。原文已删除、因违规不能看的只记一份原始数据，以后不再取。一次最多取 `--max-new` 篇（默认 5），其余留到下次。
- 归档写在 `data/inbox/<来源>/`：`md/<年>/<日期>-<sn>.md` 开头是「- 作者：」「- 时间：…(UTC+8)」「- sn：」「- 原链接：」，和以前导入的公众号归档一样；`raw/articles/<sn>.json` 是极致了的列表项。写完按 `--origin crawl` 导入（外部 id 是归档里的相对路径），图片留图床地址，由脚本接着跑的 `research-fetch-images` 下载。
- 列表查不到（key 不对、余额不足、限速）或者有文章正文两条路都取不到时退出码 1，`data/status/crawl-gudujinhuati.json`（方伟公众号是 `crawl-fangwei-mp.json`）记成失败。

### 方伟公众号老文章的图

11 月 7 日以前入库的方伟公众号文章，正文是极致了的，图按段估的位置（第 N 张图放在第 N 段后面）。`research-place-wechat-images` 按公众号原文页把图挪回原位，`scripts/crawl-fangwei-mp.sh` 每次导入之后跑一批（`PLACE_LIMIT`，默认 50 篇，新的先看）：

```bash
docker compose exec app research-place-wechat-images --source fangwei-mp --dry-run --limit 20   # 照样取原文页、算怎么挪，不写库
docker compose exec app research-place-wechat-images --source fangwei-mp --limit 50
```

- 只取原文页（`#js_content`），不花钱，两次之间隔 2 秒。字一个不改，只挪单独成行的图：两边的字只比字母、数字和汉字（标点、空白、加粗、链接地址都不算），对齐后把原文页上每张图前面的那个字对到库里的正文里，图放到离它最近的行与行之间。行里的字不拆。
- 哪张图是原文页上的哪张：图床地址里图片的 id 对得上就按 id，对不上或者是归档里带的本地文件，两边张数一样就按先后。张数不一样、先后不一样、字有一成以上对不上、有图和字在同一行，都不挪，原因记在 `raw_meta.images_checked`（这一版规则下不再取；规则改了加 `wechat_layout.LAYOUT_VERSION`，会再看一遍），阅读页照旧按识图挪。
- 挪好的和新抓的一样记 `raw_meta.images = "page"`。作者写的字没变，导入认作「只是图挪了位置」：不记旧版，观点、摘要的哈希跟过来，不重抽；检索块和提及按新正文重算（块里的字和原来一样）。归档文件不改：整目录重新导入时，库里按原文页摆好的留着，归档里同样的字、同样的图只是位置不同就不覆盖；作者改了字照常替换，替换后又等着重新摆。
- 原文已删除、因违规不能看的记下不再取。微信要验证就这次先停；连着 3 篇取不到原文页（连不上、页面认不出）也先停，这几篇不记，下次再看。只有写库出错时退出码非 0。
- 还剩多少：`--dry-run --limit 0` 打出「等着按原文页摆图的」篇数。

## 外链图片

以前从 AIHOT 导入的文章不带图，Markdown 里的 http 图片导入时也不下载，页面上原来只显示「外部图片已省略」；原文删了以后图床地址也可能失效，图表和截图就没了。`research-fetch-images` 把正文里的外链图片（Markdown 图片、`<img>` 的 `src` 和公众号懒加载的 `data-src`）下载下来，按内容哈希存进 `data/images/remote/`，文章页和备份都和归档里的图一样对待：

- 只下 http(s)，只连公网地址：每次建连接时核对对方的 IP，内网、本机、Tailscale 这类地址一律断开，跳转和中途换了解析结果也一样；最多跳转 5 次。只收图片，单张不超过 15MB。不带 Referer（公众号图床对外链 Referer 返回占位图）。
- 代理的假地址只给几个固定图床放行。Mac mini 上的代理开着 fake-ip：容器里 `i0.wp.com`、`stratechery.com` 这类国外域名解析成 `198.18.x.x`（Clash、Surge、sing-box 这类代理 fake-ip 的默认段都在 `198.18.0.0/15` 里），连上去由代理按域名转到真正的服务器。代理背后连到哪里这边看不到：正文里随便写一个域名，它真正的地址可能在内网，代理会照连。所以只有 `remote_images.PROXY_IMAGE_HOSTS` 里的图床（Stratechery 原站、`i0` 到 `i3.wp.com`、微信公众号图床 `mmbiz.qpic.cn`）解析成假地址也下，别的域名照旧不下，错误记成「地址解析到代理的假地址，不下载（域名 → 地址）」；这几个图床解析到真正的内网照样不连。微信图床 11-03 还下得下来，11-08 起 Mac mini 上也解析成假地址，新抓的公众号文章（方伟公众号 24055、24057、24061）的图一张都没下下来，文章页全是「外部图片已省略」；11-10 放行，迁移 034 删掉被拦的失败记录重排。以后有新的图床下不来、`research-status` 报「解析到代理假地址」，确认是正文里固定用的公网图床后把域名加进 `PROXY_IMAGE_HOSTS`；不想改代码，也可以在代理里把这个域名排除出 fake-ip（Clash 的 `fake-ip-filter`），容器里就拿到真地址。
- WordPress 图片 CDN 上的图（`i0.wp.com/<原站>/<路径>`，Stratechery 的图都是这样）先下原站那一张（`https://<原站>/<路径>`，不带缩放参数），下不来再试 CDN，两边的原因都记进 `image_fetches.error`。CDN 对一部分 Stratechery 的图回 404。经过：11-07 查库约 900 个地址被 fake-ip 拦、约 635 个 CDN 回 404，一张都没下下来，#256 改成先下原站、迁移 030 删掉失败记录重排；可 Mac mini 上原站 `stratechery.com` 也解析成假地址，11-09 夜里 300 个新地址照样全被拦。之后放行了上一条那几个图床的假地址，迁移 033 再删一次被拦的失败记录，从下一次下载起重新排队。
- 已经译过的英文文章，图下下来以后对照照常逐段：译文回执按不带下载图的原文核验，页面把译文里的占位换成图（见 [translate.md](translate.md)）。下下来的图和别的来源一样排进夜间识图（`NIGHTLY_OCR=1` 时每晚最多 400 张，按文章从新到旧）。
- 每个地址的结果记在 `image_fetches`：别的文章引用同一张图直接复用，不再下载；失败的按次数隔 1、3、7、30 天再试。
- 下不来的值班看得到：每次跑完，把第一次下的地址里下不来的张数、原因类别（HTTP 404、连不上、解析到代理假地址、解析到内网、域名解析不了、不是图片……）和最多的几个域名写进 `data/status/fetch-images.json`。下不来的超过一半，或者够 20 个，`research-status` 列出来，比如「外链图片下载失败（11-08 03:12）：新地址 40 个，下下来 5 个，下不来 解析到代理假地址 30、HTTP 404 5（最多的是 img.example.org 33、example.com 2）」。重试的地址以前就下不来，再失败不算；第一次下的不到 5 个又有失败时，不改上一次的结论；都下下来了就撤掉提示。另外按 `image_fetches` 查最近 3 天下过的地址：某个图床一张都没下下来（至少 3 个，原因大半不是 404、410、域名解析不了这种图床那边的事），不管这次新地址多少、是不是只有重试，都算出了问题，那句话后面接「最近 3 天一张都没下下来的图床：mmbiz.qpic.cn 12 个（解析到代理假地址 12）」，这个图床有一张下下来以后才撤。按比例看不出这种：11-08 起微信图床三天一张没下下来，每次只有几个新地址，夹在几百个下下来的 Stratechery 地址里。「解析到代理假地址」是域名不在 `PROXY_IMAGE_HOSTS` 里、被 Mac mini 上代理的 fake-ip 解析成了 `198.18.x.x`（处理见上面），「解析到内网」是解析到了真正的内网或本机，「域名解析不了」是域名没了或这台机器的 DNS 坏了。11-09 以前代理假地址和真正的内网不分，都记成「地址解析到内网或本机」，也不带域名和地址。
- 夜间补齐跑这一步带 `--exit-code`：上面那种要列出来的情况退出码 3，这一步记成失败，那句话进 `nightly.json` 的失败原因，比如「失败的步骤：外链图片（退出码 3：新地址 300 个，下下来 0 个，下不来 解析到代理假地址 300（最多的是 i0.wp.com 300））」。以前不管下下来几张都是退出码 0，11-09 夜里 300 个一张没下下来，夜间补齐照样报成功。定时抓取顺手下图时不带：那边 `run_step` 一失败整个脚本就停，会连后面的摘要都不补；它下图的结论照样写进 `fetch-images.json`。
- 导入时同步归档图片只动归档来的那些，下载来的图不会因为正文改了、重新导入而被删掉。

```bash
docker compose exec app research-fetch-images --limit 300
```

定时增量导入每次导完顺手下载新文章的图（200 张以内），夜间任务每晚再补一批（300 张以内），见 [backup.md](backup.md)。图片里的字怎么进检索见 [ocr.md](ocr.md)。

## 知识星球

期望的目录：

```text
archive/
  md/*.md
  images/
  raw/*.json
```

`raw` 里的 JSON 提供 `topic_id`、时间、标题和发帖人（`talk.owner` 或问答的 `answer.owner`）。正文以 Markdown 为准。

历史数据回填发帖人（部署后在 Mac mini 上跑，云端不对生产库跑）。网友帖的已有观点会被删掉：

```bash
docker compose exec app research-backfill-posters \
  --source fangwei-zsxq \
  --input /data/inbox/fangwei-zsxq
```

再导入一遍同一归档也会写上发帖人，即使正文没变。例：8241 这类读者帖不应再出现在方伟的茅台论点里；7723、7507 同样按发帖人名字核对。文件名里的长数字会拿去对 JSON。`raw/topics/*.json` 也要读：不能因为 `raw/group.json`、`raw/state.json` 已经让「父目录叫 raw」的列表非空，就把 `topics` 跳过，否则 `create_time` 会丢成文件名里的零点。`md/INDEX.md` 是目录，不是话题，不入库；已经在库里的，再导入或跑 `research-retime` 时会删掉。

正文开头的「- 时间：2024-11-28 10:40:22」只在没有 JSON 钟点时使用。星球导出写成「2026-09-27 14:41 (UTC+8)」，括注去掉后按上海时间读（全角、半角括号都认）。没有真实钟点就保持原样，不编造时间。

方伟星球把零点改成真实 `create_time` 之后，必须再跑一次重排。库里的 external_id 多半是 `YYYY-MM-DD_<topic_id>`，也有纯数字；按最后一个 `_` 后面的话题 id 去对 `raw/topics/<id>.json`，不改外部 id，也不编造时间。第一次部署后只改到了纯数字那 38 篇。这次部署后再跑，方伟星球应更正大约 6837 篇。

观点时间线和文章页「同期别人怎么看」按 `published_at` 现算；以前全是当天 00:00，同一天里的态度变化会标在错误的那篇上。`research-retime` 改钟点、写上 `raw_meta.create_time`，正文没变时把观点、摘要、译文的 `content_hash` 对齐（避免夜间任务把「只改了钟点」当成正文变了再抽一遍），然后按新时间把态度变化和 ±30 天的同期窗口重走一遍。不访问知识星球接口。`INDEX.md` 仍然不是话题。

```bash
docker compose exec app research-retime \
  --source fangwei-zsxq \
  --input /data/inbox/fangwei-zsxq

docker compose exec app research-retime \
  --source fangwei-zsxq \
  --input /data/inbox/fangwei-zsxq \
  --dry-run
```

手边没有归档、或者归档的 JSON 对不上时不给 `--input`：从库里存的正文开头「- 时间：」重新取，规则和导入时一样，只看库里还是当天零点（或没有日期）、又没标 `raw_meta.time_known: true` 的那些篇，只改那一行，改完的再跑不会再动。乘风破浪就是这种情况：库里没有 raw JSON 的 `create_time`（归档头写着原始数据在 `raw/topics/<话题号>.json`，导入时没对上），归档头的「(UTC+8)」以前又读不出来，库里全是当天零点，卡片和按天页的时间显示「—」，同一天的先后按入库顺序排。

```bash
docker compose exec app research-retime --source chengfengpolang --dry-run
docker compose exec app research-retime --source chengfengpolang
```

作者常在评论里回答读者，补充的判断不比正文少。JSON 里的评论只取作者本人的（问答话题里就是回答的人），连同他回复的那条读者评论，接在正文后面的「评论里的回复」一节，标签与正文同一套对话体：「网友：……」「方伟：……」（旧稿里的「读者问 / 作者答 / 作者补充」打开页面时会映射过来）。读者的名字、别的读者之间的评论都不要；作者的回复正文里已经有了的不重复。楼中楼也展开。这一节和正文一样进检索、问库和观点。

导出正文里的「## 评论（N）」在导入时就只留这个来源的星主，其他人的评论不入库。星主认来源配置的 `author` 和 `poster_names`（星球昵称）：方伟星球是方伟（显示名「方伟Rey」「方伟-最近在杭州」这些都认），乘风破浪是归档头「作者」那个名字。别的星球里的方伟也是读者。楼中楼的回复导出成「乘风破浪 回复 王小明」这种名字，看「回复」前面那个人：星主回复读者的留下，名字写成「乘风破浪 回复网友」，读者的昵称和那条读者评论都不留；读者回复星主的不留。有话题 JSON 的帖（方伟星球），星主的这些回复连同读者那句已经接进「评论里的回复」，列表里就不重复；乘风破浪的归档没有 JSON，回复留在列表里。阅读页和文章接口按同一条规则现滤，库里还没清的旧帖也不露出读者的评论。新抓的帖走同一条 `research-ingest-zsxq`，不会再把别人的评论写进正文。

标题同样在导入时收成一句：空标题、「无标题」「图片」，「下图」「如下：」这种引出图片的垫话（问答帖按话题号重导时，接口标题取的是回答开头），导出的占位标题行和下一句粘成的（「「图片」 补充一句……」），公司名/话题名加冒号的壳（「泡泡玛特：」「西部矿业：」；冒号前超过 10 个字、引出下面 1. 2. 3. 的一句话不算，比如「跟一位王者十年的游戏玩家聊天：」，去掉冒号照用），微信小程序卡片复制出来只剩一串地址的（「#小程序://雪球/…」，夹在句子里的抽句子时拿掉），以及以「…」「...」结尾（后面紧挨着引号、括号也算）的，用正文里的完整首句或问句，一般不超过 40 字。明显错字走 `textutil.fix_obvious_typos`（展示层 `present_text.polish_display_text` 同一张表，例如「很新都东西」→「很新的东西」），标题模块不另维护一份，不改观点和口语。只是错字的标题回写时直接抛光，不重抽一句。星球接口给的标题只是正文开头的十几个字、不带省略号，正文从这几个字开始又没在这里断句的，补全成那一句。接口先把开头几行用空格接起来再截，标题会跨行（23954 的「……： 混元大模型经历了不」截自说话人那一行和下一行），这时把这几行接起来补全；标题正好收在行尾的不算截断。补标题前先去掉归档头，作者、时间那几行不会混进标题。正文也是半截时，去掉省略号后还剩的主干会写进库，不再带着省略号入库。日期加「短帖」这种兜底只在页面上现算，不写入。摘要和图片里的字仍是页面和回写的后补，原标题主干优先于无关截图。评论列表的界面残屑也要修：「评论（1）」「评论(1)」，或「 - 王小明（2026-10-28 09」这种名字加钟点，以及重复的「话题：」壳贴在评论头上。前面还剩一句可用的话就切掉；只剩壳才交给 `--ai-titles`。正文里只是提到「评论」的不算。评论区里读者的评论不拿来起题，只用来源作者自己的评论（方伟星球按方伟认，乘风破浪按配置里的作者和星球昵称认）：只有图的帖正文除了占位只剩评论区，以前标题会是一位读者的网名、评论时间和那句评论（23834）。库里已经是这种「网名（2026-02-02 21:57） 那句话」的标题，页面现算时不显示，`--ai-titles` 回写时重起。只有表情或标点的标题（23855 的「😃」）也当占位。引号、书名号、括号落单的（截出来的句子只剩前引号「“估值越低，…越有利」）也要补：只差这一个符号就去掉它，原标题照用，不打 flash；开头「1）」「一）」这种编号的半个括号不算落单。摘要开头转述的引子（「作者认为」「作者借……说明：」）整个去掉，「说法」「说是」这种词不拆。正文那句以日期开头（「2026年9月1日，瑞幸咖啡宣布……」）时，收标题先去掉这个日期，卡片上本来就有日期；不去的话句子太长在逗号处收，标题只剩「2026年9月1日」。页面换了标题时，正文开头导出的旧标题行（「# 2026年9月1日」）也不显示。

这套标题规则按来源的 `kind: zsxq` 认，方伟星球和乘风破浪都用：卡片、文章页、观点时间线和文章 API 现算，不改库。识图后排进标题队列的仍只有方伟星球。乘风破浪没有抓取脚本，库里那批是这些规则以前导入的：标题还带着「...」，评论区里读者的评论和昵称也都在，检索、问库搜得到。下面回写标题和评论（评论只留星主），图片文字不动（那套整理把截图里的绿气泡记成方伟，只用在方伟的来源上）：

```bash
docker compose exec app research-repair-zsxq --source chengfengpolang --dry-run
docker compose exec app research-repair-zsxq --source chengfengpolang
```

库里已经截断的标题、还留着非方伟评论的正文、以及图片文字里的留言区，部署后在 Mac mini 上用同一套规则回写（不重新识图，不访问知识星球）：

```bash
docker compose exec app research-repair-zsxq --dry-run
docker compose exec app research-repair-zsxq
```

缺标题、壳标题、省略号/占位标题、切不掉的评论列表残屑，以及空壳导读（「没有可读文字」「本文正文为空」「无法提炼」），还可以加 `--ai-titles`，用站内当前模型（默认 Grok，跟 `RESEARCH_LLM_PROVIDER` 走）根据正文 + 图片文字 + 导读起题（失败退回上面的确定性规则）。半句截断和只是错字的不打。短正文的图片文字一旦写上，会把这篇排进标题队列，回写时排在全表扫描前面。增量抓取脚本在补摘要之后会跑：

```bash
docker compose exec app research-repair-zsxq --source fangwei-zsxq --titles-only --ai-titles --limit 30
```

抓取脚本每次把整个存档重导一遍，导入时按上面的规则重算标题。导入时没有图片里的字和导读，只有图的帖子（「「图片」」）算不出标题；这种帖子库里已经补好、正文也没改的，重导时留库里的标题，不改回占位，回写也就不会每次为同一批帖子重新起题。正文改过的照常按导入的来，由回写重新补。

存量一次补齐（大约两百多篇会打 flash，先征得同意）：

```bash
docker compose exec app research-repair-zsxq --source fangwei-zsxq --titles-only --ai-titles
```

Stratechery 全量抓取若把抓取日写进 frontmatter（例如 `published: 2026-10-21`），入库后 `published_at` 会变成上海时区当天 0 点对应的 UTC（`2026-10-20 16:00:00+00`），作者页堆在同一天。可复跑：

```bash
# 本机（需 DATABASE_URL / .env，inbox 在 data/inbox/stratechery）
PYTHONPATH=src .venv/bin/python scripts/repair_stratechery_dates.py --dry-run
PYTHONPATH=src .venv/bin/python scripts/repair_stratechery_dates.py
```

脚本按 slug 查 `stratechery.com/wp-json`，改 inbox frontmatter 与库内 `published_at`。ingest 对 `YYYY/slug` 路径若 frontmatter 年份与路径年不一致会丢掉该日期（`path_year_mismatch`），避免再次踩坑。

Stratechery 设了 `min_chars: 5500`：正文（不含 YAML 头、去掉空白）不到 5500 字符的标 `hidden=short_post`，不进首页、作者页、公司页和检索，也不译。挡的是两类：游客看到的半截 Daily Update，和每周的「This Week in Stratechery」汇总（几百词，标题借的是当周主打文章，和那篇撞名）。文章页还能打开，库里不删。

各来源的 `min_chars` 只在导入时生效。改了门槛（加、改、去掉）以后，应用启动时按新门槛把这个来源已入库的帖子重标一遍（`visibility.refresh_length_floors`，日志「…的短帖门槛改成 N 字，重标了 M 篇」），不用重新导入；别的原因隐藏的不动。

干跑会打印差表：标题回写数、评论清理数、对话重洗篇数（星球和公众号分开计）。要看某几篇改完的标题、评论和图片文字，加 `--spot 12681,473,606` 这样的文章 id（星球和公众号的都认），不给就不抽检。确认后再去掉 `--dry-run`。命令会把观点、摘要、译文的 `content_hash` 对齐到新标题，避免夜间任务把这几千篇再抽一遍。公众号只重写带页脚的 `ocr_text`，不改标题和评论。页脚按行删：粘连的「分享21 推荐0 划线人数0」「赞赏￥0」「3条留言V 写留言」，以及「微信小店」「关注同城」「相关的服务号」。按钮后面的对话留下。这次部署后再跑上面的回写，12681 不会停在被截剩的第一句，473 的小店和同城页脚也会从旁路里拿掉。

图片文字里的说话人变了以后，只重抽这几篇的观点，不要加 `--gap`，也不要全库。8881 的拼多多已经抽过，不要再放进来。

```bash
docker compose exec app research-views --articles 203,227,473,537,606
```

## Stratechery 的发布时刻

归档只记了美国当地的日历日（`published: 2025-09-22`），入库是上海当天零点，`time_known` 是 false，卡片和文章页的钟点只能写「—」（2026-11-07 线上 2409 篇全是这样）。Stratechery 官网的 WordPress 接口 `stratechery.com/wp-json/wp/v2/posts` 公开每篇的发布时刻 `date_gmt`，`research-stratechery-times` 按它补：

- 只取 slug、当地日期、发布时刻和标题，不取正文；每页 100 篇，只查还没钟点的那几段日子，第一次补全库约 30 页。
- 按外部 id 里的 slug 对上官网那一篇，官网的当地日期也得和库里的日历日一样才写；外部 id 里没有 slug 的，用标题加日历日对，同一天同名的两篇（官网有 `-2`、`-free` 这种重发）分不出来就不写。
- 写进 `published_at` 的是那个时刻，`raw_meta.time_known` 记 true，`date_source` 记 `wordpress`，原来的日历日记进 `raw_meta.calendar_day`。美国下午、晚上发的（线上约 9%）按上海时间是第二天，按天的页面上挪到第二天，和别的来源一样按上海日历。
- 观点、摘要、译文的 `content_hash` 跟着对齐（`retime.refresh_clocks`），夜间任务不会当成正文变了重抽、重译；态度变化按新时刻重走一遍，不调模型。
- 同一份只有日期的归档再导入时，`ingest.store._keep_filled_clock` 看到还是 `calendar_day` 那一天，留着库里的时刻；归档换了日期或者自己带钟点，照归档的来，夜里再按新日子补。

夜间任务第一步就是它，新导入的第二天早上就有钟点。想马上补：

```bash
docker compose exec app research-stratechery-times --dry-run
docker compose exec app research-stratechery-times
```

要退回零点：`raw_meta.calendar_day` 就是原来的日子。按它把 `published_at` 改回上海零点、去掉 `calendar_day`、`time_known` 记 false，再按原归档导入一次，观点、摘要、译文的哈希跟着对齐。

## 雪球「黑黑无常」

来源 id 是 `heiheiwuchang`，显示名和作者都是「黑黑无常」，`kind: markdown`，`enabled: true`，`timeline: true`。走现成的 `research-ingest-markdown`，不另写解析器。

这次抓取在 WAF 之后停了。磁盘上已经有的原创帖和长文（78 对 `{雪球帖子 id}.json` + 同名 `.md`）可以导入。不要再抓。转发和评论回复不在这批文件里，转换器也不访问雪球、不根据正文再筛一遍。

抓取目录是平的。转换只看这一层，不进子目录。`_summary.json`、`_STOP.json`、`_STOP.png`、`_run_meta.json`，以及任何以 `_` 开头的文件，都不是帖子，转换时跳过。正文用 JSON 的 `body_text` 全文，不截断；没有 `body_text` 时才用同名 `.md` 里标题下面的段落。这一次全部转成功后，输出目录里多余的 `.md` 会删掉，目录里只留这次的帖子。

在 Mac mini 上，把抓取目录放到 `./data/inbox/xueqiu-heiheiwuchang/`（容器里是 `/data/inbox/xueqiu-heiheiwuchang`）。`./data` 被挂到容器的 `/data`。先构建带新命令的镜像，再转换、再导入：

```bash
git pull
docker compose up -d --build

docker compose exec app research-xueqiu-markdown \
  --input /data/inbox/xueqiu-heiheiwuchang \
  --output /data/inbox/heiheiwuchang

docker compose exec app research-ingest-markdown \
  --source heiheiwuchang \
  --input /data/inbox/heiheiwuchang
```

`research-xueqiu-markdown` 把每篇写成 `/data/inbox/heiheiwuchang/{雪球帖子 id}.md`。frontmatter 里有 `id`（雪球帖子 id）、`title`、`url`、`date` 和 `published`（同一个 `published_at`，例如 `2026-04-02 19:15`）、`author: 黑黑无常`。导入器用 frontmatter 的 `id` 当外部 id，所以幂等键是 `(heiheiwuchang, 雪球帖子 id)`。再跑这两条命令不会复制；正文、标题或时间变了才更新。输出目录要和抓取目录分开，避免盖掉原始 md。不要把 `research-ingest-markdown` 直接指到抓取目录：那里的 md 没有 frontmatter，外部 id 会变成文件名（`355280496.md` 而不是 `355280496`），对不上以后的再导入。

这条导入不另外跑摘要和观点。写入时会记公司提及和段落块。新写入或改了正文的篇，若挂了图片会排进识图（一次最多 40 篇），若是英文会排进译制（一次最多 20 篇）。这批帖没有图片，识图没有可做的。摘要和观点仍不在导入时跑：打开首页时，当前这一屏缺摘要的卡片会排队；夜间任务的摘要和观点覆盖所有已启用来源，不另开、不提高。夜间识图（`NIGHTLY_OCR`）仍是没排上的图的兜底。

## 飞书和通用 Markdown

相对路径就是外部 id，所以子文档放在子目录里再次导入仍然对得上。

林文丰飞书的原文是平铺的 `docs/*.md`，知识库层级没有存下来，也不要去接飞书 wiki API。导入结束后会按正文里的相对 `.md` 链接做一遍广度优先：从「主页-Jason 林文丰」出发，第一次被谁链到，谁就是父节点；后面的交叉链接只保持为链接。结果在 `doc_tree`。已经入库、这次没有重新导入时，部署后单独跑：

```bash
docker compose exec app research-doc-tree --source linwenfeng-feishu
```

页面是 `/sources/linwenfeng-feishu/tree`。飞书不进首页按天（`timeline: false`）。文章里的相对 `.md` 链接渲染时改成 `/articles/<id>`；库里没有的去掉链接，标「未收录」。`__token` 对得上时，文件名改了也能连上。

feishu2md 把飞书的高亮块导成 GitHub 提示框：引用块第一行是 `> [!NOTE]`（还有 TIP、IMPORTANT、WARNING、CAUTION）。标记不算正文：转纯文本、切段落块时去掉，文章页显示成普通引用块。迁移 `022_alert_markers.sql` 清掉已入库正文纯文本里的标记；已有的段落块不重切，块里留着标记不影响检索。

## 导入记录和断更提示

每次导入（文件归档和定时抓取都算）都会在 `app_state` 里记下这个来源最近一次导入的时间、计数和走的哪条路（`import:<来源>`，`origin` 是 `archive` 或 `crawl`；以前从 AIHOT 导入的记作 `aihot`）。文件导入命令默认记 `archive`，定时抓取脚本加 `--origin crawl`。

首页按天的顶上，只看最近三天确实在定时抓取（`crawl`）的来源：最近一篇离现在比它平时的间隔长很多（三倍以上，至少一周）时，提示一行，并带上最近一次导入的时间。手动导入的归档本来就是旧的，不提示；最近不到 10 篇带日期的来源也不判断。定时导入的来源全都停了时只提示一行「多半是抓取本身出了问题」，不逐个列。不想要这行提示设 `RESEARCH_STALE_BANNER=0`。

## 计数报告

每次导入打印：

- 归档文章：扫到的 Markdown 篇数
- 数据库文章：这个来源当前的行数
- 写入：新增、更新、未变
- 拦下：新正文像删稿提示或被截短，没有替换库里的原文（见上面「改稿和删稿不覆盖」）
- 拒绝：没通过白名单的行（文件导入通常是 0）
- 重复：写入后认出是库里已有的同一篇
- 归档图片、新保存图片、挂上文章的图片、缺失图片引用

图片按内容哈希存在 `data/images/<来源>/<哈希>`。同一张图不会存两份。

## 示例数据

```bash
ALLOW_SAMPLE_SEED=1 research-seed-sample
```

示例文章标题带「示例」，页面顶部会提示这不是付费原文。生产库不要开 `ALLOW_SAMPLE_SEED`。

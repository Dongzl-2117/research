# 按当时可见的 SEC 披露复核指标

`research-sec-facts` 读取一家公司 SEC Company Facts 的原始 JSON。它按发行主体 CIK、XBRL 标签、原报单位和完整报告期查找可核对的数值，与公司观点、预测、行情分开保存。查询和手记复核在本机离线运行。

## 查询一项披露

仓库内这条命令可以直接用合成样本运行。安装项目后也可以用 `research-sec-facts` 代替 `python -m researchlib.sec_facts`。

```sh
PYTHONPATH=src python -m researchlib.sec_facts observe tests/fixtures/sec/companyfacts.json \
  --cik 1234567 --tag us-gaap:Revenue --unit USD \
  --start 2024-01-01 --end 2024-03-31 --before 2024-05-01
```

`--before` 是不包含当天的纽约日历日，上例只读 `filed < 2024-05-01` 的披露。只纳入申报日已结束的报告期，排除尚未实现的未来期间。报告期由事实自身的 `start` 和 `end` 确定。同一个季末可能同时存在本季和年初至今两个数。查资产等时点指标时省略 `--start`。数值是保留原始精度的字符串，缺失返回 `status: missing`。

`history` 列出每次符合条件的申报，逐条给出表单类型、申报日、accession、数值和 SEC 申报索引，索引内可打开原始报表。同一 accession、同一期间的重复记录会合并，同一申报内数值矛盾会报错。原始值、修订表单及后续报表的同期比较值都可见，是否为重述要看原文。最大披露日有多份申报时返回 `ambiguous_same_day`，保留列表，`latest` 留空。

## 取得原始快照

可以自己从 SEC 下载 JSON 再离线分析。需要命令下载时填写自己的机构或姓名、真实联系邮箱，为每次快照取一个新的文件名。

```sh
research-sec-facts fetch --cik "$CIK" --user-agent "$SEC_USER_AGENT" --output "$SNAPSHOT"
```

`fetch` 每次只请求固定的 data.sec.gov 地址一次。macOS 或 Linux 上同一用户共享限速，每次请求结束后至少间隔一秒。403 或 429 后至少暂停十分钟，较长的 Retry-After 优先。快照保留返回的原始字节。多台机器共享出口时须共同遵守 SEC fair access 规则。请把快照、手记和生成的文件留在自己的私有目录。[SEC 数据 API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) · [SEC 开发者访问规则](https://www.sec.gov/about/developer-resources)

## 把实绩带入投研手记

在手记里先保存自己的判断、预期、指标和单位。时点指标的报告期填 `YYYY-MM-DD`，期间指标填 `YYYY-MM-DD/YYYY-MM-DD`。导出手记备份，从备份中取对应的 `note:…` 编号。运行上面的 `observe`，打开选中行的申报索引，核对公司、原表对应行、原报单位、accession 和 `Accepted` 的时间。下例中的 CIK、标签、日期和 accession 对应合成样本，实际运行时换成自己的材料。

```sh
research-sec-facts journal "$SNAPSHOT" \
  --cik 1234567 --tag us-gaap:Revenue --unit USD \
  --start 2024-01-01 --end 2024-03-31 --before 2024-05-01 \
  --backup "$BACKUP" --note-id "$NOTE_ID" --company '示例' --metric '营收' \
  --accession 0000999999-24-000001 --published-at '2024-04-30T19:05:06-04:00' \
  --output "$PATCH"
```

公司的名称和指标名按手记填写。CIK 与证券、XBRL 标签与投资指标的对应关系由复核人确认。命令同时检查备份中的公司、指标、单位、期间，并核对 accession。Company Facts 只有申报日期。填写索引中核实的美东接受时刻及其时区，手记将其保存为这份 SEC 资料的公开时间。如果 `Accepted` 与 `filed` 落在不同的纽约日期，保留离线观察结果，跨日核对由研究员在手记中手工完成。

将输出的 v1 JSON 在「手记 → 备份与恢复 → 导入 JSON」打开即可。新版本的记录时间取本次操作时刻，原始假设、预期、上一版实绩和其他手记保留。已经存过的同一实绩不会新增版本，已有更晚的披露或同一时间有不同数值时会报错。先保留原始备份可独立恢复。

## 适用范围

Company Facts 主要覆盖 SEC 电子申报中标准分类下的实体级 XBRL 事实。交易代码、ADR 比率、跨市场同名公司、币种换算、分部、非 GAAP 自定义口径、港股和 A 股本地财报需要各自的一手资料。使用多币种或标签更换前后的数据，要单独判断可比性。一个标签查不到时返回缺失。

用新下载的公司快照回看旧日，只能重建该份快照仍保留的历史申报。若要证明研究员在某日实际捕获了哪些数据，请在当时保存原始快照和下载日期，保留申报索引及原文。盘中复盘、财报发布会或交易所提前发布的时间，需要按对应一手资料单独核对。

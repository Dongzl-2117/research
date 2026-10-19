"""主要 A 股、港股、美股和未上市的公司种子。

同一别名只能属于一家公司。过短或容易撞进普通词的别名不收，
例如不用单独的「平安」「理想」「药明」。规则见 docs/mentions.md。
"""

from __future__ import annotations

from typing import NamedTuple


class CompanySeed(NamedTuple):
    ticker: str | None
    name_zh: str
    name_en: str
    market: str
    aliases: tuple[str, ...]


def _company(
    ticker: str | None,
    name_zh: str,
    name_en: str,
    market: str,
    *aliases: str,
) -> CompanySeed:
    return CompanySeed(ticker, name_zh, name_en, market, aliases)


SEEDS: tuple[CompanySeed, ...] = (
    # 持仓和观察的公司补了业务名，讲 Temu、Labubu、视频号、微信的文章也算提到这家公司。
    # 「微信」会收；公众号模板里的「微信扫一扫」等由 mentions.scrub_publisher_noise 滤掉。
    _company("00700.HK", "腾讯", "Tencent", "HK", "腾讯控股", "TCEHY", "0700.HK", "视频号", "微信", "WeChat"),
    _company("09988.HK", "阿里巴巴", "Alibaba", "HK", "阿里", "BABA"),
    _company("03690.HK", "美团", "Meituan", "HK", "美团点评"),
    _company("09618.HK", "京东", "JD.com", "HK", "京东集团", "JD"),
    _company("09888.HK", "百度", "Baidu", "HK", "BIDU"),
    _company("09999.HK", "网易", "NetEase", "HK", "NTES"),
    _company("01810.HK", "小米", "Xiaomi", "HK", "小米集团"),
    _company("01024.HK", "快手", "Kuaishou", "HK"),
    _company("09626.HK", "哔哩哔哩", "Bilibili", "HK", "B站", "BILI"),
    _company("09868.HK", "小鹏汽车", "XPeng", "HK", "小鹏", "XPEV"),
    _company("09866.HK", "蔚来", "NIO", "HK"),
    _company("02015.HK", "理想汽车", "Li Auto", "HK", "LI"),
    _company("09633.HK", "农夫山泉", "Nongfu Spring", "HK", "东方树叶"),
    _company("09992.HK", "泡泡玛特", "Pop Mart", "HK", "POPMART", "Labubu", "拉布布"),
    _company("02020.HK", "安踏体育", "Anta", "HK", "安踏"),
    _company("02331.HK", "李宁", "Li Ning", "HK"),
    _company("06690.HK", "海尔智家", "Haier", "HK", "海尔"),
    _company("00388.HK", "香港交易所", "HKEX", "HK", "港交所"),
    _company("00941.HK", "中国移动", "China Mobile", "HK"),
    _company("02269.HK", "药明生物", "WuXi Biologics", "HK"),
    _company("01801.HK", "信达生物", "Innovent", "HK"),
    _company("09961.HK", "携程", "Trip.com", "HK", "携程集团", "TCOM"),
    _company("02423.HK", "贝壳", "Beike", "HK", "贝壳找房", "BEKE"),
    _company("PDD", "拼多多", "Pinduoduo", "US", "PDD Holdings", "Temu"),
    _company(None, "字节跳动", "ByteDance", "PRIVATE", "字节"),
    _company("AAPL", "苹果", "Apple", "US", "苹果公司", "iPhone"),
    _company("AMZN", "亚马逊", "Amazon", "US"),
    _company("MSFT", "微软", "Microsoft", "US"),
    _company("GOOGL", "谷歌", "Alphabet", "US", "Google"),
    _company("META", "Meta", "Meta Platforms", "US", "Facebook", "脸书"),
    _company("NVDA", "英伟达", "NVIDIA", "US"),
    _company("TSLA", "特斯拉", "Tesla", "US"),
    _company("TSM", "台积电", "TSMC", "US"),
    _company("AMD", "超威半导体", "AMD", "US"),
    _company("AVGO", "博通", "Broadcom", "US"),
    _company("ASML", "阿斯麦", "ASML", "US"),
    _company("QCOM", "高通", "Qualcomm", "US"),
    _company("INTC", "英特尔", "Intel", "US"),
    _company("NFLX", "奈飞", "Netflix", "US"),
    _company("JPM", "摩根大通", "JPMorgan", "US"),
    _company("BRK.B", "伯克希尔", "Berkshire", "US", "BRK.B"),
    _company("COST", "开市客", "Costco", "US", "好市多"),
    _company("KO", "可口可乐", "Coca-Cola", "US"),
    _company("DIS", "迪士尼", "Disney", "US"),
    _company("NKE", "耐克", "Nike", "US"),
    _company("SBUX", "星巴克", "Starbucks", "US"),
    _company("WMT", "沃尔玛", "Walmart", "US"),
    _company("LLY", "礼来", "Lilly", "US", "Eli Lilly"),
    _company("UNH", "联合健康", "UnitedHealth", "US"),
    _company("600519.SH", "贵州茅台", "Kweichow Moutai", "A", "茅台"),
    _company("000858.SZ", "五粮液", "Wuliangye", "A"),
    _company("000568.SZ", "泸州老窖", "Luzhou Laojiao", "A"),
    _company("600809.SH", "山西汾酒", "Shanxi Fenjiu", "A", "汾酒"),
    _company("601318.SH", "中国平安", "Ping An", "A", "平安保险", "02318.HK"),
    _company("000001.SZ", "平安银行", "Ping An Bank", "A"),
    _company("600036.SH", "招商银行", "China Merchants Bank", "A", "招行"),
    _company("601398.SH", "工商银行", "ICBC", "A", "工行", "01398.HK"),
    _company("601288.SH", "农业银行", "Agricultural Bank of China", "A", "农行", "01288.HK"),
    _company("601988.SH", "中国银行", "Bank of China", "A", "03988.HK"),
    _company("601166.SH", "兴业银行", "Industrial Bank", "A"),
    _company("600030.SH", "中信证券", "CITIC Securities", "A"),
    _company("300059.SZ", "东方财富", "East Money", "A"),
    _company("600900.SH", "长江电力", "China Yangtze Power", "A"),
    _company("601899.SH", "紫金矿业", "Zijin Mining", "A", "紫金"),
    _company("601088.SH", "中国神华", "China Shenhua", "A", "神华"),
    _company("600028.SH", "中国石化", "Sinopec", "A", "中石化", "00386.HK"),
    _company("601857.SH", "中国石油", "PetroChina", "A", "中石油", "00857.HK"),
    _company("600938.SH", "中国海油", "CNOOC", "A", "中海油", "00883.HK"),
    _company("002594.SZ", "比亚迪", "BYD", "A", "01211.HK"),
    _company("300750.SZ", "宁德时代", "CATL", "A"),
    _company("601012.SH", "隆基绿能", "LONGi", "A", "隆基股份", "隆基"),
    _company("002475.SZ", "立讯精密", "Luxshare", "A", "立讯"),
    _company("002371.SZ", "北方华创", "NAURA", "A"),
    _company("688981.SH", "中芯国际", "SMIC", "A", "中芯", "00981.HK"),
    _company("688041.SH", "海光信息", "Hygon", "A", "海光"),
    _company("603986.SH", "兆易创新", "GigaDevice", "A", "兆易"),
    _company("002230.SZ", "科大讯飞", "iFlytek", "A", "讯飞"),
    _company("000333.SZ", "美的集团", "Midea", "A", "美的", "00300.HK"),
    _company("000651.SZ", "格力电器", "Gree", "A", "格力"),
    _company("600276.SH", "恒瑞医药", "Hengrui", "A", "恒瑞"),
    _company("603259.SH", "药明康德", "WuXi AppTec", "A", "02359.HK"),
    _company("300760.SZ", "迈瑞医疗", "Mindray", "A", "迈瑞"),
    _company("300015.SZ", "爱尔眼科", "Aier", "A"),
    _company("601888.SH", "中国中免", "China Tourism Group Duty Free", "A", "中免"),
    _company("002714.SZ", "牧原股份", "Muyuan", "A", "牧原"),
    _company("600887.SH", "伊利股份", "Yili", "A", "伊利"),
    _company("002352.SZ", "顺丰控股", "SF Holding", "A", "顺丰"),
    _company("002415.SZ", "海康威视", "Hikvision", "A", "海康"),
    _company("000725.SZ", "京东方", "BOE", "A"),
    _company("002027.SZ", "分众传媒", "Focus Media", "A", "分众"),
    _company("300274.SZ", "阳光电源", "Sungrow", "A"),
    _company("601127.SH", "赛力斯", "Seres", "A", "问界"),
    _company("002460.SZ", "赣锋锂业", "Ganfeng Lithium", "A", "赣锋"),
    _company("300014.SZ", "亿纬锂能", "EVE Energy", "A", "亿纬"),
)


MARKETS = frozenset({"A", "HK", "US", "PRIVATE"})
MARKET_LABELS = {
    "A": "A股",
    "HK": "港股",
    "US": "美股",
    "PRIVATE": "未上市",
}

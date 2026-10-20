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
    _company("600028.SH", "中国石化", "Sinopec", "A", "中石化", "中国石油化工", "00386.HK"),
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


# 有歧义的别名。问题里的公司名只受 exclude 约束；文章里还要看 needs 和 conflicts。
# exclude：落在这些写法里的命中不算（正则），例如「烟台苹果」「中美的关系」「中国银行业」。
# needs：这个别名要有佐证词（或这家公司的其他写法）在同一篇里才算。
# conflicts：给了的话，只有同一篇里出现这些词（说明可能在讲别的东西）时才要佐证；
#   没给就一律要佐证。「苹果」只在讲水果、农产品的文章里要求佐证，讲苹果公司的短帖不受影响。
ALIAS_RULES: dict[str, dict] = {
    "苹果": {
        "needs": {
            "苹果": [
                "iPhone", "iPad", "iOS", "macOS", "Mac", "MacBook", "iMac", "AirPods", "Vision Pro", "App Store",
                "Apple Watch", "iCloud", "Siri", "库克", "乔布斯", "苹果手机", "苹果链", "果链", "苹果产业链",
                "苹果供应链", "苹果生态", "苹果税", "智能手机", "高端手机", "手机业务", "手机市场", "手机销量",
                "手机出货", "手机厂商", "手机行业", "手机品牌", "平板电脑", "应用商店", "富士康", "鸿海", "立讯",
            ],
        },
        "conflicts": {
            "苹果": [
                "水果", "果农", "果园", "鲜果", "果品", "农产品", "农副产品", "生鲜", "产区", "种植", "亩产", "农户",
                "收购价", "一斤", "每斤", "元/斤", "公斤", "红富士", "冰糖心", "橙子", "脐橙", "柑橘", "香蕉", "梨子",
                "猕猴桃", "葡萄", "草莓", "西瓜",
            ],
        },
        # 只排除明确是水果的写法。「苹果收购」「苹果园区」「苹果核心」这类是公司，不能排除。
        "exclude": [
            r"(?:烟台|阿克苏|洛川|静宁|栖霞|昭通|盐源|天水|延安|陕西|山东|新疆|甘肃|红富士|富士|嘎啦|蛇果|丑|冰糖心|糖心"
            r"|有机|新鲜|一斤|斤|一箱|筐|吃|削|啃|摘)苹果",
            r"17\.5\s*[°度]?\s*苹果",
            r"苹果(?:汁|醋|树|派|味|酱|脆片|罐头|种植|产区|果农|丰收|滞销|苗)",
            r"苹果园(?!区)",
            r"苹果核(?!心)",
        ],
    },
    "贵州茅台": {
        "exclude": [r"茅台(?:镇|机场|学院|县)"],
    },
    "美的集团": {
        # 单独的「美的」在不谈家电的文章里多半是形容词，一律要佐证。
        "needs": {
            "美的": [
                "空调", "家电", "冰箱", "洗衣机", "小天鹅", "库卡", "KUKA", "方洪波", "何享健", "美的置业", "美的电器",
                "白电", "格力", "海尔", "暖通",
            ],
        },
        # 「中美的」「欧美的」「完美的」「最美的」里都有「美的」。「很多美的产品」「格力和美的」照算。
        "exclude": [
            r"(?:中|欧|日|英|北|南|拉|赴|留|旅|访|完|最|很|更|太|真|甜|优|精|华|柔|唯|审|健|俊|秀|丰|鲜|肥|甘|娇"
            r"|凄|赞|绝|貌|爱|臭|选|两全其|物)美的",
        ],
    },
    "字节跳动": {
        "needs": {
            "字节": [
                "抖音", "TikTok", "今日头条", "头条", "飞书", "豆包", "火山引擎", "张一鸣", "梁汝波", "西瓜视频",
                "Pico", "剪映", "巨量引擎", "红果",
            ],
        },
        "conflicts": {
            "字节": [
                "字节码", "内存", "编码", "字符", "比特", "二进制", "数据类型", "千字节", "兆字节", "指针", "编译",
            ],
        },
        # 只排除计算机里的「字节」。「很多字节跳动的员工」整词是字节跳动，不受影响。
        "exclude": [r"\d\s*字节", r"字节(?:码|数|流|序|对齐|长度)", r"(?:千|兆|吉|个|多|单|双|四|八)字节"],
    },
    "小米": {
        "needs": {
            "小米": [
                "雷军", "小米手机", "小米汽车", "小米之家", "米家", "Redmi", "红米", "MIUI", "澎湃OS", "SU7", "YU7",
                "智能手机", "手机业务", "手机市场", "手机销量", "手机出货", "手机厂商", "手机品牌", "IoT", "生态链",
                "米粉",
            ],
        },
        "conflicts": {
            "小米": ["粥", "杂粮", "粮食", "五谷", "食谱", "谷子", "小米辣", "小米椒"],
        },
        "exclude": [r"小米(?:粥|辣|椒|饭|稀饭|酒)", r"(?:黄|熬)小米", r"小米加步枪"],
    },
    "贝壳": {
        "needs": {
            "贝壳": [
                "链家", "彭永东", "左晖", "经纪人", "房产经纪", "二手房", "新房", "房产中介", "中介", "租赁", "房源",
                "家装",
            ],
        },
        "conflicts": {
            "贝壳": ["海边", "沙滩", "海滩", "海洋", "贝类", "珍珠", "化石", "软体动物", "海螺"],
        },
        "exclude": [r"贝壳(?:类|粉|饰|画)", r"(?:捡|海|拾)贝壳"],
    },
    "海光信息": {
        # 「上海光大」「珠海光库」「威海光威」里都有「海光」。
        "exclude": [r"[上珠威]海光"],
    },
    "中国移动": {
        # 「中国移动互联网」「中国移动支付」说的是中国的移动互联网，不是中国移动这家公司。
        "exclude": [r"中国移动(?:互联网|支付|游戏|电商|广告|应用|端|视频|办公|医疗|社交|设备)"],
    },
    "伊利股份": {
        # 伊利诺伊州、伊利亚（人名）、伊利湖。
        "exclude": [r"伊利(?:诺伊|诺斯|亚|湖|里亚)"],
    },
    "紫金矿业": {
        "exclude": [r"紫金(?:山|港|农商|保险|财险|县|街|城|阁)"],
    },
    "阿里巴巴": {
        "exclude": [r"阿里(?:山|地区|郎)", r"·阿里|阿里·"],
    },
    "亚马逊": {
        "exclude": [r"亚马逊(?:雨林|河|流域|森林|地区|丛林)"],
    },
    "中国银行": {
        # 「中国银行业」「中国银行保险监督管理委员会」「中国银行间市场」不是中国银行。
        "exclude": [r"中国银行(?:业|保险|间)"],
    },
    "中国石油": {
        # 中国石化的全称「中国石油化工」由中国石化的别名接住（同一位置取最长）。
        "exclude": [r"中国石油(?:大学|和化学|学会)"],
    },
    "中国石化": {
        "exclude": [r"中国石化联合会"],
    },
}


MARKETS = frozenset({"A", "HK", "US", "PRIVATE"})
MARKET_LABELS = {
    "A": "A股",
    "HK": "港股",
    "US": "美股",
    "PRIVATE": "未上市",
}

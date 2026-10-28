"""把微信截图的 OCR / 识图结果整理成博客式对话。

微信里绿色气泡是作者自己（方伟），通常靠右、上面不写名字；灰色气泡是别人，通常靠左、上面有昵称。
RapidOCR 有原图时按绿气泡底色给本人行加侧标记，没有图时退回靠右几何。识图若已经写成「方伟：」「网友：」，
再跑一遍仍剥界面字，不把「可按住转文字 / 继续提问 / AI搜索」这类 UI 行标成说话人。不在这里调用模型。

- 靠右（或没有坐标时、不带别人昵称）的气泡 →「方伟：」
- 别人的昵称（王小明、李小红、Kevin，以及只写了名字的）→「网友：」，正文里不再留这个名字
- 显示名「方伟 / 方伟Rey / 方伟-最近在杭州」仍是方伟。句子里的 @方伟 不是说话人
- 方伟连续说的合成一块；网友每次开口仍分开，不因为都标成网友就并成一段
- 雪球 / 微博嵌套回复不是从上到下的时间序：``回复@`` / ``//@`` 从早到晚展开，下面的引用卡片放到回复前面
- 跨图重复的句子只留一次；后一张图只是把上一句又认了一遍时，新的半句接回上一块
- 群名、价格标签、小程序、设为群待办、状态栏时间电量这些界面字去掉
- 知识星球和公众号的页脚按行丢掉，后面的对话留下，不从第一行按钮截到图末。
  「分享21 推荐0 划线人数0」「赞赏￥0」「3条留言V 写留言」「微信小店」「关注同城 朋友 推荐」都是页脚。
  「李四留言1次·关注8个月」换成网友的一轮。以下为历史留言、共N条回复、留言N次、关注N个月删掉。
  「饮料快暴 +关注」「品饮汇 品饮汇观察 +关注」「观察 56个朋友关注 82 455 38 33。」这类账号名、
  按钮和阅读量粘在一行的页脚也整行丢掉；正文里提到「朋友关注」的句子不动。
- 知识星球回复头的「方伟在学习（作者）2」是说话人分界：下面是方伟，上面仍是上一轮。
  微信小店 / 拼多多 / 淘宝的商品卡截图不套对话，阅读页只留原图链接；正文句子里的「微信小店」照常保留。

财报表格、贴成长文的图没有这些说话人或微信界面，就原样留下，不套对话。
知识星球引用回复不是微信气泡：第一条「方伟看十年 / 作者」账号栏是分界，栏上的灰字是「网友：」，
栏下的回复是「方伟：」。账号栏本身和卡片末尾的下一帖标题去掉。栏上没有正文、一上来就是账号栏的，
仍整段是方伟。留言区是另一类版式，不靠这条分界。
电商商品图、带坐标轴的图表、数据表、Reddit 等外网评论截图不套对话，阅读页只留原图链接。
已经整理过的「方伟：」「网友：」再跑一遍还是同一份，方便写回 articles.ocr_text。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

HOST = "方伟"
GUEST = "网友"
# 靠右的绿色气泡。零宽连字，str.strip 去不掉，整理进对话时拿掉，不显示。
SIDE_SELF = "\u2060"

_CJK = re.compile(r"[一-鿿]")
_MEANING = re.compile(r"[0-9A-Za-z一-鿿]")
_STOP_CHAR = frozenset(
    "的了着过和与或而且是不在把被让给到就也还很最这那你我他她它中但吧吗啊呢呀哦嗯又从对为以会能要说看想做化性"
)
# 单独成行、或冒号前面的这些字是话，不是昵称。
_NOT_NAMES = frozenset(
    {
        "同意",
        "好的",
        "好",
        "收到",
        "哈哈",
        "哈哈哈",
        "嗯",
        "嗯嗯",
        "是的",
        "对",
        "对的",
        "谢谢",
        "感谢",
        "明白",
        "了解",
        "确实",
        "有道理",
        "学习了",
        "马克",
        "赞",
        "可以",
        "不行",
        "不是",
        "是",
        "哦",
        "啊",
        "额",
        "行",
        "中",
        "认同",
        "顶",
        "看到了",
        "同问",
        "同求",
        "牛",
        "厉害",
        "注意",
        "所以",
        "但是",
        "如果",
        "不过",
        "另外",
        "还有",
        "就是",
        "这个",
        "那个",
        "我们",
        "他们",
        "你们",
        "现在",
        "今天",
        "昨天",
        "因为",
        "然后",
        "还是",
        "应该",
        "觉得",
        "认为",
        "表示",
        "说明",
        "总结",
        "比如",
        "例如",
        "或者",
        "以及",
        "关于",
        "对于",
        "根据",
        "为了",
        "因此",
        "于是",
        "最后",
        "首先",
        "其实",
        "当然",
        "可能",
        "已经",
        "只有",
        "虽然",
        "而且",
        "并且",
        "同时",
        "瑞幸",
        "古茗",
    }
)
# 显示名可以带状态后缀：方伟-最近在杭州、方伟Rey、方伟（星主）。句子里的「方伟说得对」不是名字。
_HOST_LINE = re.compile(
    r"^(?:"
    r"方\s*伟(?:\s*Rey)?"
    r"(?:\s*[-－—]\s*[^\s：:，。！？!?]{1,16})?"
    r"(?:\s*[（(][^）)]{0,8}[）)])?"
    r"|Rey"
    r")$",
    re.I,
)
_HOST_PREFIX = re.compile(r"^(方\s*伟(?:\s*Rey)?|Rey)\s+(.+)$", re.I)
_STRUCTURED = re.compile(r"^(方伟|网友)\s*[:：]\s*(.*)$")
_INLINE = re.compile(r"^(.{1,16}?)[:：]\s*(.+)$")
# 群名不一定带「群」字，例如「1633 示例·线下深度交流(80)」。
_GROUP_TITLE = re.compile(
    r"^(?:\d{1,6}\s+)?[^\n（(]{0,24}群\s*[（(]\d{2,4}[）)]$"
    r"|^(?:\d{2,6}\s+).{2,40}[（(]\d{1,4}[）)]$"
    # 「看十年价投交流(499)」：无「群」字、无未读数前缀的群标题。
    r"|^[\u4e00-\u9fffA-Za-z0-9·\s]{2,24}[（(]\d{2,4}[）)]$"
)
_LATIN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,19}$")
_LATIN_STOP = frozenset(
    {"gpt", "opus", "claude", "codex", "grok", "the", "and", "for", "you", "are", "not", "but"}
)
_GROUP_BARE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]{2,16}群$")
_TIME = re.compile(
    r"^(?:上午|下午|晚上|凌晨|昨天|今天|前天|星期[一二三四五六日天])?\s*\d{1,2}[:：]\d{2}(?:[:：]\d{2})?[≥>≤<]*$"
    r"|^(?:昨天|今天|前天|星期[一二三四五六日天])$"
    r"|^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s*\d{1,2}[:：]\d{2})?[≥>≤<]*$"
    r"|^\d{1,2}月\d{1,2}日(?:\s*\d{1,2}[:：]\d{2})?[≥>≤<]*$"
)
_STATUS = re.compile(
    r"^(?:微信|WeChat|返回|发送|按住\s*说话|以下是新消息|消息免打扰)$"
    r"|^\d{1,3}\s?%$"
    r"|^[45]G$"
    r"|^(?:LTE|Wi-?Fi|VPN|HD|NFC)$",
    re.I,
)
_CHROME_WORD = re.compile(
    r"^(?:[^\u4e00-\u9fffA-Za-z0-9]{0,6})?"
    r"(?:设为群待办|设为待办|群待办|小+程序|进入小程序|查看详情|来自小程序|"
    r"回到引用位置|可按住\s*转文字|按住\s*转文字|"
    r"继续提问|AI\s*搜索|加[人入]?会员群|雪球|(?i:LIVE))"
    r"(?:\s*[A-Za-z])?[。．.\s]*$"
)
# ￥17.? 、￥19.?￥22泰式风味红茶：价格可以粘在商品名上，问号是小数点认丢了。
_PRICE = re.compile(
    r"^(?:券后价?\s*)?(?:[¥￥$]\s*\d+(?:\.\d*)?[\?？]*\s*)+(?:[\u4e00-\u9fffA-Za-z]{0,16})?$"
    r"|^\d+\.\d{1,2}\s*元(?:起)?$"
)
_STORE = re.compile(r"^.{1,24}?\s*[（(][^（）()]{1,16}店[）)]$")
_COMMERCE = re.compile(r"美团外卖|外卖美食|交易保障")
_APP_FRAGMENT = re.compile(r"^QuestMobile", re.I)
_AT_SUFFIX = re.compile(r"@[A-Za-z0-9_\-.]+$")
_SYSTEM = re.compile(r"撤回了一条消息|拍了拍|以上是打招呼|你已添加")
_SYMBOL = re.compile(r"^[\s<>+=…\.。,，、·]+$")
_WECHAT = re.compile(
    r"微信|WeChat|设为群待办|设为待办|群待办|小+程序|按住\s*说话|"
    r"可按住\s*转文字|按住\s*转文字|回到引用位置|以下是新消息|美团外卖"
)
# 公众号文章预览页的「账号名 +关注」：账号名和按钮 OCR 粘在一行，旧规则只认整行「+关注」。
# 「观察 56个朋友关注 82 455 38 33。」是「N个朋友关注」粘着阅读量、点赞数。
# 「交易规则 71个朋友关注 53 47 23 写留言」再把留言按钮粘在末尾。
_FOLLOW_BUTTON = re.compile(r"^.{0,20}[+＋]关注[。．.]?$")
_FRIEND_FOLLOW = re.compile(
    r"^[^\d\s。！？!?，,；;：:]{0,10}\d+个朋友关注[\d\s。．.、，,]*(?:写留言|留言|查看全部留言)?$"
)
# 知识星球回复头「方伟在学习（作者）2」：这一行下面是方伟的话，不是网友。
_HOST_BADGE = re.compile(r"^(?P<name>(?:方\s*伟|Rey)[^（()]{0,10})[（(]作者[）)]\d*$")
# 朋友圈 / 公众号卡片上的账号栏。不是气泡里的话。
# 知识星球引用回复里，第一条这样的行把栏上的灰字和栏下的回复切开。
_ACCOUNT_BADGE = re.compile(
    r"^方伟看十年作者$|^看十年作者$"
    r"|^方伟看十年[（(]作者[）)]$"
    r"|^看十年[（(]作者[）)]$"
)
_ACCOUNT_NAME = re.compile(r"^方伟看十年$|^看十年$")
# 公众号名片上单独一行的「（作者）」。不是正文，也不是引用回复的分界。
_AUTHOR_BADGE = re.compile(r"^[（(]作者[）)]$")
_AUTHOR_INLINE = re.compile(r"[（(]作者[）)]")
# 帖子截图上的按钮行。只删这一行，后面的对话还在。
# 长的放前面，避免「留言」吃掉「写留言」「条留言」。
_STICKY_TOKEN = re.compile(r"划线人数|写留言|条留言|条评论|写评论|赞赏|分享|推荐|收藏|在看|点赞|留言|划线")
_STRONG_FOOTER = re.compile(
    r"^(?:"
    r"(?:\d+)?条留言"
    r"|(?:\d+)?条评论"
    r"|写留言"
    r"|写评论"
    r"|留言"
    r"|划线人数\d*(?:人)?"
    r"|\d+人划线"
    r"|划线\d+"
    r"|查看全部留言|查看更多留言|更多留言|全部留言|留言列表"
    r"|作者赞过"
    r")$"
)
_SOFT_FOOTER = re.compile(r"^(?:赞赏(?:一下)?|去赞赏|分享(?:到(?:朋友圈|微信))?|转发给朋友|收藏)$")
_GLUED_FOOTER = re.compile(r"^(?:(?:\d+)?条留言|写留言|划线人数\d*|赞赏|分享|收藏){2,}$")
_LIST_CHROME = re.compile(
    r"^(?:查看全部|查看更多|更多|全部|展开)?留言(?:列表)?$"
    r"|^(?:\d+)?条评论$"
    r"|^写评论$"
    r"|^作者赞过$"
)
# 「李四留言」「李四留言1次·关注8个月」「孙小平SunPing留言1次·关注3个月」「UI留言17次·新的关注」。
_COMMENT_HEAD = re.compile(
    r"^(?P<name>(?:[\u4e00-\u9fff]{1,8}[A-Za-z][A-Za-z0-9_]{0,40}|UI|[\u4e00-\u9fff]{1,8}))留言"
    r"(?:"
    r"[:：](?P<said>.*)"
    r"|(?P<ui>(?:\d+次)?(?:[·•・‧∙･．.]*(?:新的关注|关注\d+个?月|关注))?)"
    r")$"
)
_COMMENT_UI = re.compile(
    r"^(?:"
    r"以下为历史留言|查看历史留言|历史留言"
    r"|共\d+条(?:回复|留言|评论)"
    r"|留言\d+次"
    r"|[·•・‧∙･．.]*关注\d+个?月"
    r"|[·•・‧∙･．.]*新的关注"
    r")$"
)
_TIME_PLACE = re.compile(
    r"^(?:昨天|今天|前天|上午|下午|晚上|凌晨)?\d{1,2}:\d{2}(?::\d{2})?[\u4e00-\u9fff]{2,4}$"
)
# 「李四留言」：这一轮改记成网友，名字本身不显示。
_GUEST_TURN = "\x1d"
# 「方伟在学习（作者）」：这一行下面是方伟的话，上面仍是上一轮。
_HOST_TURN = "\x1c"
_LABELED_REST = re.compile(r"^(?:方\s*伟|网友)\s*[:：]\s*(?P<rest>.+)$")
_COMMENT_HEAD_SKIP = frozenset({"写", "欢迎", "请", "大家", "请大家", "条", "我要", "的", "这", "那"})
# 下一帖标题常粘在卡片末尾：短问句、正文里没有句号。
_FEED_TITLE = re.compile(
    r"^(?:[^。！\n]{4,40}[？?]|什么才叫做[^。！？\n]{0,30})$"
)
# 亚马逊 / Temu 商品页界面字。有这些就不走对话。
_PRODUCT_UI = re.compile(
    r"Sponsored|Add\s*to\s*cart|Addtocart|FREEdelivery|bought\s*in\s*past\s*month|"
    r"boughtfrom\s*thisbrand|Limited\s*time\s*deal|Typical:|AllDiscounts|"
    r"MostPurchased|Shop[A-Z]|购物车|秒杀特价|平台包邮|无进口费用|"
    r"筛选|已售\d|已额外优惠",
    re.I,
)
# 图表角标、轴标签。OCR 出来是乱码，阅读页不展示。
_CHART_MARK = re.compile(r"Builtwith|Fiscalai|Fiscal\.ai|McP$|Ads\s*v\.?Google|LTM\)", re.I)
# 微信小店 / 电商 App 的商品列表和商品卡：店铺 tabs、商品卡按钮、促销坑位。
# 「大家都在搜」「百亿补贴」在微博、小红书、京东外卖群里也常见，不能单独当商店页。
_SHOP_STRONG = re.compile(
    r"小店介绍|精选\s*商品\s*分类|全店总售|立即参与万人团|品牌\s*收藏\s*客服|"
    r"微信小店助手|微信小店交易规则中心"
)
_SHOP_WEAK = re.compile(r"点赞补贴价|上滑继续看视频|去参与")
# 知识星球优惠券 / 立减金推广卡。账号栏「方伟看十年」会把下面误标成方伟。
_ZSXQ_COUPON = re.compile(r"扫码领取优惠券|立减金")
_ZSXQ_COUPON_EXTRA = re.compile(r"仅限前\s*\d+\s*名|\d+\s*元立减金")
# 微博评论卡头：小事7747 山东 2
_WEIBO_HEADER = re.compile(
    r"^[A-Za-z0-9_\u4e00-\u9fff]{2,20}\s+[\u4e00-\u9fff]{2,3}\s+\d{1,4}$"
)
_WEIBO_FEED_FOOTER = re.compile(
    r"^(?:[\u4e00-\u9fff]{2,8}(?:新闻)?联播|[\u4e00-\u9fff]{2,8}日报|[\u4e00-\u9fff]{2,8}新闻)$"
)
# 雪球/微博帖头：2025-10-31 09:35 · 浙江 · 2022年度十大影响力用户
_FEED_META = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(?:\s*[·•・‧∙･．.]\s*.+)?$"
)
_INFLUENCE_BADGE = re.compile(
    r"(?:年度)?十大影响力用户|超级[\u4e00-\u9fffA-Za-z0-9]{2,16}|雪球\d{4}年度"
)
# 点赞/评论/转发数字栏：756 1555 827
_ENGAGEMENT_COUNTS = re.compile(r"^\d{1,6}(?:\s+\d{1,6}){1,4}$")
_FEED_ACTION = re.compile(r"^(?:发讨论|查看对话)[>〉›]?$")
_VIDEO_FOLLOWED = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9_\-]{2,20}\s+)?已关注$"
)
_VIDEO_CTA = re.compile(
    r"(?:"
    r"欢迎关注\s*@?\S{0,40}\s*视频号"
    r"|点击\s*#\S{1,40}\s*查看更多"
    r"|作者提示\s*[:：]\s*内容为转载"
    r"|更多.{0,24}欢迎关注"
    r")"
)
# 主播名牌：「主播 浩嘉」。名字只收两三个字，长句里的「主播」留下。
_HOST_NAMEPLATE = re.compile(
    r"^(?:主播|嘉宾)[\u4e00-\u9fff]{2,3}$|^(?:主持人|出镜记者)[\u4e00-\u9fff]{2,3}$"
)
# 视频角上的法定标、直播标。整行才算，句子里提到「未成年人」不删。
_VIDEO_CORNER = re.compile(
    r"^(?:未成年人请勿饮酒|请勿酒驾|酒后请勿驾车|过量饮酒有害健康|理性饮酒|直播中|正在直播)$"
)
# 节目角标，只在已经认出视频壳之后丢掉最后一行。「会稽山超越古越龙山」这类标题不收。
_SHOW_BUG = re.compile(r"^[\u4e00-\u9fff]{2,6}(?:观察|视界|频道)$")
# 广告海报顶部的对仗口号：「双重保障，终身守护。」条款正文更长，对不上。
_AD_SLOGAN = re.compile(r"^[\u4e00-\u9fff]{2,6}[，,][\u4e00-\u9fff]{2,6}。$")
_BRAND_MASTHEAD = re.compile(
    r"^(?:领教工坊|INSIDE\s+THE\s+BEST|COLMO)$",
    re.I,
)
_SOURCE_MASTHEAD = re.compile(
    r"^来源\s*[:：]\s*(?:思想[?？×xX]?岭南|领教工坊)\b"
)
_REPLY_AT_MARK = re.compile(r"回复@|//@")
# 时间行隔开两段话。文字丢掉，但要断开上一句，不能把下一位的话接上去。
_BREAK = "\x1e"
_MP_TOKEN = re.compile(
    r"微信小店|关注同城|相关的服务号|相关服务号|关注服务号|进店逛逛|关注公众号|视频号|同城"
)
_MP_FILLER = re.compile(r"朋友|推荐|分享|在看|收藏|进店|关注|去看看|助手")
# 第一条账号栏。栏上已经收成网友，栏下改按方伟。本身不显示。
_ACCOUNT_SPLIT = "\x1f"
_TRAIL_TIME = re.compile(
    r"\s*(?:上午|下午|晚上|凌晨|昨天|今天|前天)?\s*\d{1,2}:\d{2}(?::\d{2})?\s*$"
)
_SUFFIX = re.compile(
    r"[\s,，]*?(?:设为群待办|设为待办|群待办|回到引用位置|"
    r"可按住\s*转文字|按住\s*转文字|继续提问|AI\s*搜索)\s*$"
)
_PUNCT = re.compile(r"[，。！？、；：,.!?;:\"“”‘’（）()\[\]【】《》<>]")


@dataclass
class Turn:
    speaker: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Shot:
    """一张图：对话轮次、只留原图的商品/图表，或保持原样的正文（表格、长文）。"""

    kind: str
    turns: list[Turn]
    prose: str
    # dialogue 子类：moments / group / chat。朋友圈与群聊不要合成一块。
    family: str = ""


def present_ocr_pieces(pieces: list[str]) -> str:
    """多张图按顺序合成一篇旁路正文。连续的群聊合并、去重；表格仍是原文。"""
    blocks: list[str] = []
    pending: list[Turn] = []
    seen: list[str] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        text = render_turns(pending, seen)
        pending = []
        if text.strip():
            blocks.append(text.strip())

    pending_family = ""
    for piece in pieces:
        shot = parse_shot(piece)
        if shot.kind == "dialogue":
            family = shot.family or "chat"
            if pending and pending_family and family != pending_family:
                flush()
            pending.extend(shot.turns)
            pending_family = family
            continue
        flush()
        pending_family = ""
        prose = (shot.prose or "").strip()
        if prose:
            blocks.append(prose)
            for line in prose.splitlines():
                norm = _norm(line)
                if len(norm) >= 8 and norm not in seen:
                    seen.append(norm)
    flush()
    text = "\n\n".join(blocks)
    text = re.sub(r"一\?会", "一年后会", text)
    text = re.sub(r"一\?后", "一年后", text)
    from researchlib.present_text import polish_display_text
    from researchlib.textutil import collapse_short_spam

    text = collapse_short_spam(text)
    return polish_display_text(text)


def present_stored_ocr(stored: str) -> str:
    """库里可能还是按图拼接的原始识别，也可能已经是对话。两种再跑都得到对话。"""
    from researchlib.present_text import polish_display_text
    from researchlib.textutil import collapse_short_spam

    text = (stored or "").strip()
    if not text:
        return ""
    pieces = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return polish_display_text(collapse_short_spam(present_ocr_pieces(pieces or [text])))


def speaker_note(text: str) -> str:
    """摘要和观点的提示里补一句说话人，没有对话标记时不加。"""
    if "方伟：" in (text or "") or "网友：" in (text or ""):
        return "群聊已整理成「方伟：」和「网友：」。方伟是星主，其余人都写成网友，不要把网友的话写成方伟的判断。"
    return ""


def _plain(line: str) -> str:
    return line[len(SIDE_SELF) :] if line.startswith(SIDE_SELF) else line


def _footer_compact(line: str) -> str:
    return re.sub(r"\s+", "", _fold(_plain(line)))


def _is_author_badge(line: str) -> bool:
    """单独的「（作者）」，或「张三（作者）」这种短署名。账号栏留给引用回复去分界。"""
    plain = _plain(line)
    if _account_line(plain):
        return False
    text = _footer_compact(line)
    if _AUTHOR_BADGE.fullmatch(text):
        return True
    if not text or len(text) > 16 or re.search(r"[。！？!?]", text):
        return False
    return _AUTHOR_INLINE.search(text) is not None


def _sticky_residue(compact: str) -> str:
    """去掉按钮词、计数、币符和 OCR 粘上的单个拉丁字母，看还剩多少正文。"""
    text = _STICKY_TOKEN.sub("", compact)
    text = re.sub(r"[\d０-９￥¥$€A-Za-z]+", "", text)
    return re.sub(r"[\s._·•,，。！？!?：:；;、~～\-—+|｜/\\（）()【】\[\]]+", "", text)


def _is_sticky_bar(line: str) -> bool:
    """一行里粘着好几个按钮，或按钮粘着计数。

    「分享21 推荐0 划线人数0」「赞赏￥0」「3条留言V 写留言」整行都是界面。
    「这个赞赏的设计并不值钱」去掉「赞赏」还剩一句，留下。
    """
    compact = _footer_compact(line)
    if not compact or len(compact) > 48 or re.search(r"[。！？!?，,]", compact):
        return False
    if _LIST_CHROME.fullmatch(re.sub(r"\d+$", "", compact)):
        return True
    hits = _STICKY_TOKEN.findall(compact)
    if not hits:
        return False
    has_meter = re.search(r"\d|[￥¥$]", compact) is not None
    exact = _STRONG_FOOTER.fullmatch(compact) is not None or _GLUED_FOOTER.fullmatch(compact) is not None
    if len(hits) < 2 and not has_meter and not exact:
        return False
    return _CJK.findall(_sticky_residue(compact)) == []


def _is_time_place(line: str) -> bool:
    """评论时间戳加地名，例如「22:07上海」。句子里夹着钟点的不走这里。"""
    return _TIME_PLACE.fullmatch(_footer_compact(line)) is not None


def _is_follow_chrome(line: str) -> bool:
    """「+关注」「14个朋友关注 3 19。」以及账号名/按钮粘在一行的情况。

    「饮料快暴 +关注」「品饮汇 品饮汇观察 +关注」「观察 56个朋友关注 82 455 38 33。」
    都是公众号预览页的页脚，整行丢掉。正文里提到朋友关注的（「我有14个朋友关注这件事」）不删。
    """
    text = _footer_compact(line)
    if not text or len(text) > 40:
        return False
    button = _FOLLOW_BUTTON.fullmatch(text)
    if button:
        # 按钮前面只能是账号名；带句读的是一句话，不是页脚。
        return re.search(r"[。！？!?，,；;：:]", text[:-3]) is None
    return _FRIEND_FOLLOW.fullmatch(text) is not None


def _is_host_badge(line: str) -> bool:
    """知识星球回复头的「方伟在学习（作者）2」：下面是方伟的回复。

    「方伟看十年（作者）」是引用回复的账号栏，留给 _ACCOUNT_SPLIT 分界，不能当普通回复头。
    """
    compact = _footer_compact(line)
    match = _HOST_BADGE.fullmatch(compact)
    if not match:
        return False
    return not _account_line(match.group("name"))


def _labeled_guest_header(line: str) -> str | None:
    """「方伟：孙小平SunPing留言1次·关注3个月」里，冒号后面才是留言头。"""
    plain = _plain(line).strip()
    match = _LABELED_REST.match(plain)
    if not match:
        return None
    rest = match.group("rest").strip()
    if _is_comment_head(rest):
        return rest
    return None


def _is_comment_ui(line: str) -> bool:
    """「以下为历史留言」「共2条回复」「留言1次」「关注8个月」。整行都是界面。"""
    compact = _footer_compact(line)
    if not compact or re.search(r"[。！？!?，,]", compact):
        return False
    return _COMMENT_UI.fullmatch(compact) is not None


def _is_mp_footer(line: str) -> bool:
    """公众号底部的小店、同城、服务号，以及「关注同城 朋友 推荐」。

    句子里提到这些词的留下。
    """
    compact = _footer_compact(line)
    if not compact or len(compact) > 24 or re.search(r"[。！？!?，,]", compact):
        return False
    anchored = re.search(r"微信小店|关注同城|服务号|进店逛逛|视频号|关注公众号", compact)
    city_row = "同城" in compact and re.search(r"朋友|推荐", compact)
    if not anchored and not city_row:
        return False
    residue = _MP_TOKEN.sub("", compact)
    residue = _MP_FILLER.sub("", residue)
    residue = re.sub(r"[\dA-Za-z]+", "", residue)
    return _CJK.findall(residue) == []



def _is_feed_meta(line: str) -> bool:
    """雪球/微博帖头时间·地区·影响力行。

    「超级鹿鼎公」这类昵称会命中超级…，不能单凭徽章词当帖头；要有日期行，
    或整行是「十大影响力用户 / 雪球年度」说明。
    """
    plain = _plain(line).strip()
    if not plain:
        return False
    if _FEED_META.fullmatch(plain):
        return True
    if re.search(r"[。！？!?]", plain) or len(plain) > 48:
        return False
    # 单独的影响力说明行（不含「超级某某」昵称）
    if re.search(r"(?:年度)?十大影响力用户|雪球\d{4}年度", plain):
        return True
    return False


def _is_engagement_counts(line: str) -> bool:
    return _ENGAGEMENT_COUNTS.fullmatch(_plain(line).strip()) is not None


def _is_feed_action(line: str) -> bool:
    return _FEED_ACTION.fullmatch(_plain(line).strip()) is not None


def _compact_speaker(line: str) -> str:
    return re.sub(r"\s+", "", _speaker_body(line))


def _is_broadcast_line(line: str) -> bool:
    """视频主播名牌、角标、直播标。整行才算。"""
    compact = _compact_speaker(line)
    if not compact or len(compact) > 16:
        return False
    return _HOST_NAMEPLATE.fullmatch(compact) is not None or _VIDEO_CORNER.fullmatch(compact) is not None


def _drop_ad_shell(lines: list[str]) -> list[str]:
    """品牌广告海报的 Logo 和口号。条款、适用条件留下。

    只在同一张图里已经有「适用条件」和保障/免责说明时动手，避免把正文里的短句删掉。
    书籍目录没有这种形状，整段留下。
    """
    blob = "\n".join(_speaker_body(line) for line in lines)
    if "适用条件" not in blob or not re.search(r"责任免除|官方渠道|终身保障", blob):
        return lines
    out: list[str] = []
    dropped_logo = False
    for index, line in enumerate(lines):
        body = _speaker_body(line)
        compact = re.sub(r"\s+", "", body)
        nxt = _speaker_body(lines[index + 1]) if index + 1 < len(lines) else ""
        if (
            not dropped_logo
            and index < 2
            and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,4}", compact)
            and len(re.sub(r"\s+", "", nxt)) >= 6
        ):
            dropped_logo = True
            continue
        if index < 4 and _AD_SLOGAN.fullmatch(body.strip()):
            continue
        out.append(line)
    return out


def drop_broadcast_chrome(lines: list[str]) -> list[str]:
    """视频角标、主播条、节目水印，以及品牌广告的 Logo/口号。新闻标题和条款留下。"""
    if not lines:
        return lines
    lines = _drop_ad_shell(lines)
    if not any(_is_broadcast_line(line) for line in lines):
        return lines
    kept = [line for line in lines if not _is_broadcast_line(line)]
    if len(kept) >= 2 and _SHOW_BUG.fullmatch(_compact_speaker(kept[-1])):
        kept = kept[:-1]
    return kept


def _is_video_cta(line: str) -> bool:
    """视频号关注 CTA / 已关注条 / 作者转载提示。"""
    plain = _plain(line).strip()
    if not plain:
        return False
    body = re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", plain)
    if _VIDEO_FOLLOWED.fullmatch(body):
        return True
    if _VIDEO_CTA.search(body):
        return True
    return False


def _is_feed_chrome(line: str) -> bool:
    """雪球/微博/视频号壳：帖头元信息、发讨论、数字栏、关注 CTA；杂志品牌刊头；视频角标。"""
    if (
        _is_feed_meta(line)
        or _is_engagement_counts(line)
        or _is_feed_action(line)
        or _is_video_cta(line)
        or _is_broadcast_line(line)
    ):
        return True
    body = re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", _plain(line).strip())
    if _BRAND_MASTHEAD.fullmatch(body) or _SOURCE_MASTHEAD.match(body):
        return True
    return False


def _is_xueqiu_author_nick(line: str, nxt: str) -> bool:
    """短昵称行，下一行是时间·地区·影响力帖头。"""
    plain = _plain(line).strip()
    if not plain or len(plain) > 16 or re.search(r"[。！？!?，,；;：:]", plain):
        return False
    if re.search(r"\d{4}-\d{2}-\d{2}", plain):
        return False
    if _STRUCTURED.match(plain) or plain in (_GUEST_TURN, _HOST_TURN):
        return False
    nxt_plain = _plain(nxt).strip()
    # 必须是带日期的帖头行，避免标题下一行是「超级某某」昵称时误删标题
    return bool(nxt_plain) and _FEED_META.fullmatch(nxt_plain) is not None


def _collapse_reply_at(line: str) -> str:
    """「回复@甲:正文 //@乙:…」压成正文；保留已有「网友：/方伟：」前缀。"""
    plain = (line or "").strip()
    if not plain:
        return ""
    prefix = ""
    labeled = re.match(r"^((?:方伟|网友)\s*[:：]\s*)", plain)
    if labeled:
        prefix = labeled.group(1)
        plain = plain[labeled.end() :]
    if not _REPLY_AT_MARK.search(plain):
        return line.strip()
    parts = re.split(r"\s*//@\s*", plain)
    for part in parts:
        part = re.sub(r"^(?:回复)?@[^\s:：]+[:：]\s*", "", part.strip()).strip()
        if part:
            return f"{prefix}{part}".strip()
    return ""


def _is_strong_footer(line: str) -> bool:
    text = _footer_compact(line)
    if not text:
        return False
    if _is_feed_chrome(line):
        return True
    if (
        _is_sticky_bar(text)
        or _is_mp_footer(line)
        or _is_comment_ui(line)
        or _is_time_place(line)
        or _is_follow_chrome(line)
    ):
        return True
    if len(text) > 24:
        return False
    return _STRONG_FOOTER.fullmatch(text) is not None or _GLUED_FOOTER.fullmatch(text) is not None


def _is_soft_footer(line: str) -> bool:
    text = _footer_compact(line)
    return bool(text) and len(text) <= 16 and _SOFT_FOOTER.fullmatch(text) is not None


def _comment_head_match(line: str) -> re.Match[str] | None:
    return _COMMENT_HEAD.fullmatch(_footer_compact(line))


def _name_cjk(name: str) -> str:
    return "".join(_CJK.findall(name))


def _comment_name_ok(name: str) -> bool:
    """汉字名，或汉字名后粘着拉丁备注（孙小平SunPing），或头像认成的 UI。"""
    if name == "UI":
        return True
    if not name or name in _COMMENT_HEAD_SKIP or name.isdigit():
        return False
    cjk = _name_cjk(name)
    if not cjk or len(cjk) > 8 or any(char in _STOP_CHAR for char in cjk):
        return False
    if _is_host_line(_compact_host(cjk)):
        return False
    return re.fullmatch(r"[\u4e00-\u9fff]{1,8}(?:[A-Za-z][A-Za-z0-9_]{0,40})?", name) is not None


def _is_comment_head(line: str) -> bool:
    """「李四留言」「李四留言1次·关注8个月」。句子里的「请在下面留言」不是。"""
    match = _comment_head_match(line)
    if not match:
        return False
    return _comment_name_ok(match.group("name"))


def _post_chrome_context(lines: list[str]) -> bool:
    """这张图是星球 / 公众号帖子，而不是群里有人随口说了「赞赏」。"""
    for line in lines:
        if _is_strong_footer(line) or _is_comment_head(line) or _is_author_badge(line):
            return True
        if _account_line(_plain(line)):
            return True
    return False


def _is_message_label(line: str) -> bool:
    return _footer_compact(line) in {"留言", "写留言"}


def _bare_guest_name(line: str) -> bool:
    text = _footer_compact(line)
    if not text or _is_host_line(_compact_host(text)):
        return False
    return _cjk_name(text, 2) or _cjk_name(text, 3) or _latin_name(text)


def _split_comment_head(line: str) -> tuple[str, str] | None:
    """留言头改成网友的一轮。次、关注月数丢掉；冒号后面的话留下。"""
    plain = _plain(line).strip()
    match = _comment_head_match(plain)
    if not match or not _comment_name_ok(match.group("name")):
        return None
    said = match.group("said")
    if said is None:
        return ("", "")
    rest = said.strip()
    if rest and (_is_sticky_bar(rest) or _is_strong_footer(rest) or _is_comment_ui(rest)):
        rest = ""
    return ("", rest)


def _comment_name_line(line: str) -> bool:
    """评论区里单独一行的人名，例如「孙小平」。"""
    text = _footer_compact(line)
    if not text or text in _NOT_NAMES or _is_host_line(_compact_host(text)):
        return False
    if _cjk_name(text, 2) or _cjk_name(text, 3):
        return True
    chars = _CJK.findall(text)
    return len(chars) == 4 and len(chars) == len(text) and not any(char in _STOP_CHAR for char in chars)


def _following_speech(line: str) -> bool:
    text = _plain(line).strip()
    if not text or text == _GUEST_TURN or _is_host_line(_compact_host(text)):
        return False
    if _is_comment_ui(text) or _is_strong_footer(text) or _is_comment_head(text) or _is_mp_footer(text):
        return False
    return len(_CJK.findall(text)) >= 6 or re.search(r"[。！？!?，,]", text) is not None


def _peel_sticky_tail(line: str) -> str:
    """正文句号后面直接粘上「分享21 推荐0 划线人数0」时，只留下正文。整行都是按钮的不动，交给截断。"""
    if _is_sticky_bar(line) or _is_strong_footer(line):
        return line
    own = line.startswith(SIDE_SELF)
    plain = _plain(line).strip()
    if not plain:
        return line
    for match in _STICKY_TOKEN.finditer(plain):
        head = plain[: match.start()].rstrip()
        tail = plain[match.start() :]
        if not head or not _is_sticky_bar(tail):
            continue
        if re.search(r"[。！？!?]$", head) or len(_CJK.findall(head)) >= 4:
            return (SIDE_SELF if own else "") + head
    return line


def _drop_post_footer(lines: list[str]) -> list[str]:
    """页脚按行丢掉。按钮后面的方伟、网友继续留着，不从第一行按钮截到图末。

    「回复@ / //@」若还留在一行里，按时间从早到晚拆开，不只留最新一条。

    「李四留言」和「李四留言1次·关注8个月」改成网友的一轮，名字和关注月数不进正文。
    视频角标、主播名牌和广告海报的 Logo/口号也在这里剥掉。
    """
    from researchlib.present_text import expand_reply_chain_line

    lines = drop_broadcast_chrome(lines)
    peeled = [_peel_sticky_tail(line) for line in lines]
    context = _post_chrome_context(peeled)
    kept: list[str] = []
    in_comments = False
    index = 0
    while index < len(peeled):
        line = peeled[index]
        nxt = peeled[index + 1] if index + 1 < len(peeled) else ""
        if _is_xueqiu_author_nick(line, nxt):
            index += 1
            continue
        if _is_feed_chrome(line):
            index += 1
            continue
        if _bare_guest_name(line) and _is_message_label(nxt):
            kept.append(_GUEST_TURN)
            in_comments = True
            index += 2
            continue
        embedded = _labeled_guest_header(line)
        if embedded:
            comment = _split_comment_head(embedded)
            kept.append(_GUEST_TURN)
            if comment and comment[1]:
                kept.append(comment[1])
            in_comments = True
            index += 1
            continue
        host_head = _comment_head_match(line)
        if host_head and _is_host_line(_compact_host(_name_cjk(host_head.group("name")))):
            index += 1
            continue
        comment = _split_comment_head(line)
        if comment is not None:
            _name, rest = comment
            kept.append(_GUEST_TURN)
            if rest:
                kept.append(rest)
            in_comments = True
            index += 1
            continue
        if _is_comment_ui(line):
            in_comments = True
            index += 1
            continue
        if in_comments and _comment_name_line(line) and _following_speech(nxt):
            kept.append(_GUEST_TURN)
            index += 1
            continue
        if _is_host_badge(line):
            # 知识星球回复头：上面那轮收住，下面是方伟。
            kept.append(_HOST_TURN)
            index += 1
            continue
        if _is_author_badge(line) or _is_strong_footer(line) or (context and _is_soft_footer(line)):
            index += 1
            continue
        expanded = expand_reply_chain_line(line)
        if expanded is not None:
            kept.extend(item for item in expanded if item.strip())
            index += 1
            continue
        collapsed = _collapse_reply_at(line)
        if collapsed:
            kept.append(collapsed)
        index += 1
    return kept


def _prose_after_footer(original: str, raw_lines: list[str], stripped: list[str]) -> Shot:
    from researchlib.present_text import merge_wrapped_utterances

    if not stripped:
        return Shot("media", [], "")
    if stripped == raw_lines:
        body = original
    else:
        body = "\n".join(_plain(line) for line in stripped).strip()
    return Shot("prose", [], merge_wrapped_utterances(body))


def _is_zsxq_coupon_shot(lines: list[str]) -> bool:
    """知识星球扫码领券 / 立减金推广卡：整段丢掉，不能标方伟。"""
    joined = "\n".join(lines)
    if re.search(r"按规则整段不输出|优惠券推广卡", joined):
        return True
    if not _ZSXQ_COUPON.search(joined):
        return False
    # 短卡，或同时带「仅限前 / N元立减金」
    if _ZSXQ_COUPON_EXTRA.search(joined):
        return True
    return len(re.findall(r"[一-鿿]", joined)) <= 60


def _is_weibo_comment_shot(lines: list[str]) -> bool:
    """微博/资讯评论卡：昵称+地区+数字，正文是评论。"""
    if not lines:
        return False
    head = _plain(lines[0]).strip()
    if not _WEIBO_HEADER.fullmatch(head):
        return False
    return any(len(_plain(line).strip()) >= 8 and _CJK.search(line) for line in lines[1:])


def _weibo_guest_turns(lines: list[str]) -> list[Turn]:
    body: list[str] = []
    for index, line in enumerate(lines):
        plain = _plain(line).strip()
        if index == 0 and _WEIBO_HEADER.fullmatch(plain):
            continue
        if _WEIBO_FEED_FOOTER.fullmatch(plain):
            continue
        if _is_feed_chrome(plain):
            continue
        plain = _collapse_reply_at(plain)
        if plain:
            body.append(plain)
    if not body:
        return []
    return [Turn(speaker=GUEST, lines=body)]


def parse_shot(text: str) -> Shot:
    from researchlib.present_text import order_nested_replies

    original = order_nested_replies((text or "").strip())
    if not original:
        return Shot("prose", [], "")
    raw_lines = [_scrub(line) for line in _normalize(original).splitlines()]
    raw_lines = [line for line in raw_lines if line and not _is_ui_chrome(line)]
    if not raw_lines:
        return Shot("prose", [], "")
    # 知识星球优惠券推广卡：账号栏会把「扫码领取…」误标方伟，整段当噪音。
    if _is_zsxq_coupon_shot(raw_lines):
        return Shot("media", [], "")
    # 微博/资讯评论卡：昵称行改成网友，不要留「小事7747」这类名字。
    if _is_weibo_comment_shot(raw_lines):
        turns = _weibo_guest_turns(raw_lines)
        if turns:
            return Shot("dialogue", turns, "", "chat")
        return Shot("media", [], "")
    # 商品图、图表、外网评论截图、数据表先拦下：不能进微信对话。
    # 识图已标「方伟：/网友：」的群聊截图可能内嵌估值表——不能当纯表整段丢掉。
    labeled_chat = any(_STRUCTURED.match(_plain(line) or "") for line in raw_lines)
    if not labeled_chat and (
        _is_product_shot(raw_lines)
        or _is_shop_shot(raw_lines)
        or _is_chart_shot(raw_lines)
        or _is_table_shot(raw_lines)
        or _is_foreign_comment_shot(raw_lines)
    ):
        return Shot("media", [], "")
    stripped = _drop_post_footer(raw_lines)
    signal = _wechat_signal(stripped)
    lines = _prepare_lines(stripped)
    names = _speaker_names(lines, signal)
    # 已经是「方伟：」「网友：」的块再跑也要能合并，不能因为界面字已经被拿掉就退回原文。
    visible = [
        _plain(line) for line in lines if line not in (_BREAK, _ACCOUNT_SPLIT, _GUEST_TURN, _HOST_TURN)
    ]
    labeled = bool(visible) and _STRUCTURED.match(visible[0]) is not None
    hosted = (
        any(_is_host_line(line) for line in visible)
        or any(line.startswith(SIDE_SELF) for line in lines)
        or _HOST_TURN in lines
    )
    if not hosted:
        for line in visible:
            if _HOST_PREFIX.match(line) or (_STRUCTURED.match(line) and line.startswith(HOST)):
                hosted = True
                break
            inline = _INLINE.match(line)
            if inline and _is_host_line(_compact_host(inline.group(1))):
                hosted = True
                break
    if not lines or not (signal or names or labeled or hosted):
        return _prose_after_footer(original, raw_lines, stripped)
    # 群聊里没有坐标时，不写别人昵称的气泡是作者自己的绿色气泡。有侧标记时以标记为准。
    turns = _walk(lines, names, self_unnamed=bool(signal or names or hosted))
    had_account = any(_account_line(_plain(line)) for line in stripped)
    family = "moments" if had_account else ("group" if signal else "chat")
    if turns:
        return Shot("dialogue", turns, "", family)
    # 整张都是界面字时不要把原文退回去，否则价格和小程序会漏进正文。
    if signal or names or hosted or labeled:
        return Shot("dialogue", [], "", family)
    return _prose_after_footer(original, raw_lines, stripped)


def _is_product_shot(lines: list[str]) -> bool:
    """亚马逊 / Temu 商品页：价格、Sponsored、购物车。不是群聊。"""
    joined = "\n".join(lines)
    hits = _PRODUCT_UI.findall(joined)
    if len(hits) >= 2:
        return True
    dollars = len(re.findall(r"(?<![A-Za-z])\$\s*\d", joined))
    stars = len(re.findall(r"★", joined))
    return dollars >= 3 and stars >= 2


def _is_shop_shot(lines: list[str]) -> bool:
    """微信小店 / 拼多多 / 淘宝的商品卡截图。

    店铺 tabs（小店介绍、精选 商品 分类）、商品卡按钮（全店总售、立即参与万人团）或
    店铺页脚是强信号。弱信号（点赞补贴价、上滑继续看视频、去参与）要同时出现「微信小店」。
    正文里提到「微信小店」四个字不会命中这些界面词，照常保留。
    """
    joined = "\n".join(lines)
    if _SHOP_STRONG.search(joined):
        return True
    if "微信小店" not in joined:
        return False
    return _SHOP_WEAK.search(joined) is not None and re.search(r"[￥¥]\s*\d", joined) is not None


def _is_chart_shot(lines: list[str]) -> bool:
    """折线图 / 柱状图的 OCR：角标、轴刻度，读出来也没有句子。"""
    if any(_CHART_MARK.search(line) for line in lines):
        return True
    digitish = 0
    meaningful = 0
    for line in lines:
        compact = re.sub(r"\s+", "", line)
        if not compact:
            continue
        if re.fullmatch(r"[\d$￥¥B.,~\-—]+", compact) or re.fullmatch(r"\d{6,}", compact):
            digitish += 1
        if len(_MEANING.findall(line)) >= 6:
            meaningful += 1
    return digitish >= 4 and digitish > meaningful


_REDDIT_HANDLE = re.compile(
    r"(?m)^[A-Za-z][\w-]{1,30}[·.]\d+(?:mo|d|h|y|yr|hr|hour|day|month|year)?\s*ago\b",
    re.I,
)
_REDDIT_HANDLE_COMPACT = re.compile(
    r"(?m)^[A-Za-z][\w-]{1,30}[·.]\d+(?:mo|d|h|y)ago\b",
    re.I,
)
_REDDIT_UI = re.compile(r"(?m)^(?:Share|Reply|Award|Comment|Upvote|Downvote)\b", re.I)


def _is_foreign_comment_shot(lines: list[str]) -> bool:
    """Reddit / 外网评论楼：用户名+3moago、Share/Reply，不是微信群。"""
    joined = "\n".join(lines)
    handles = _REDDIT_HANDLE.findall(joined) + _REDDIT_HANDLE_COMPACT.findall(joined)
    if handles:
        return True
    if len(_REDDIT_UI.findall(joined)) >= 2 and sum(1 for c in joined if c.isascii()) > len(joined) * 0.7:
        return True
    return False


def _is_table_shot(lines: list[str]) -> bool:
    """SCF / 电商份额等数据表：多列数字，阅读页只留原图。"""
    if any(re.search(r"指标\s*数值|公司\s*财年|净利率|经营利润率", line) for line in lines):
        return True
    rowish = 0
    for line in lines:
        if re.search(r"\d", line) and (
            re.search(r"\S+\s{2,}\S+", line) or re.search(r"(?:约)?[\d,.]+\s*(?:万|亿|%|美元)", line)
        ):
            rowish += 1
    return rowish >= 4 and rowish * 5 >= len(lines) * 3


def _same_sentence(left: str, right: str) -> bool:
    """两句去掉标点后相同，或几乎整句被另一张图又认了一遍。

    短很多的前半句不算重复：方伟另起的「会有一批大模型达到」不能被上一张里更长的那行吃掉。
    """
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) < 8 or len(right) < 8:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if shorter not in longer:
        return False
    return len(shorter) * 5 >= len(longer) * 4


def render_turns(turns: list[Turn], seen: list[str] | None = None) -> str:
    """去重后写成「方伟：」「网友：」。

    方伟连续说的并成一块。网友每次开口各自一块，避免把好几个人粘成一段后，
    后一句不再以「网友：」开头。后一张图若只是重叠了上一句，剩下的新行接回上一块。
    seen 跨图累计，就地更新。
    """
    kept_norms = seen if seen is not None else []
    rows: list[tuple[str, str, str, int, int]] = []
    for turn_id, turn in enumerate(turns):
        for pos, line in enumerate(turn.lines):
            cleaned = line.strip()
            if cleaned:
                rows.append((turn.speaker, cleaned, _norm(cleaned), turn_id, pos))
    norms = [row[2] for row in rows]
    # 折行会留下单独一个「的」。空行和纯标点仍然丢掉。
    keep = [len(norm) >= 1 for norm in norms]
    for index, norm in enumerate(norms):
        if not keep[index]:
            continue
        for earlier in range(index):
            if not keep[earlier]:
                continue
            old = norms[earlier]
            if _same_sentence(norm, old):
                keep[index] = False
                break
            if len(old) >= 8 and old in norm and len(norm) > len(old):
                keep[earlier] = False
    chosen: list[tuple[str, str, str, int, int]] = []
    first_pos: dict[int, int] = {}
    kept_pos: dict[int, int] = {}
    for _speaker, _line, _norm_line, turn_id, pos in rows:
        first_pos.setdefault(turn_id, pos)
    for index, row in enumerate(rows):
        if not keep[index]:
            continue
        speaker, line, norm, turn_id, pos = row
        if any(_same_sentence(norm, old) for old in kept_norms):
            continue
        kept_norms.append(norm)
        kept_pos.setdefault(turn_id, pos)
        chosen.append(row)
    blocks: list[list] = []
    for speaker, line, _norm_line, turn_id, _pos in chosen:
        continued = kept_pos.get(turn_id, 0) > first_pos.get(turn_id, 0)
        if blocks and blocks[-1][2] == turn_id:
            blocks[-1][1].append(line)
            continue
        if blocks and speaker == HOST and blocks[-1][0] == HOST:
            blocks[-1][1].append(line)
            blocks[-1][2] = turn_id
            continue
        if blocks and continued and speaker == blocks[-1][0]:
            blocks[-1][1].append(line)
            blocks[-1][2] = turn_id
            continue
        blocks.append([speaker, [line], turn_id])
    return "\n\n".join(f"{speaker}：{_close_block(lines)}" for speaker, lines, _turn in blocks)


def _close_block(lines: list[str]) -> str:
    """一块话的末尾补上句号，标题和去重才能在说话人交界处停住。已经有句号的不再加。"""
    body = "\n".join(lines)
    # OCR 常把句号认成半角点，例如「仅供参.」。这种不再补一个全角句号。
    if body and body[-1] not in "。！？!?….":
        body += "。"
    return body


def _normalize(text: str) -> str:
    """只统一换行和看不见的字符。中文标点留在原文里，不把「，」折成半角逗号。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)


def _fold(line: str) -> str:
    """判断界面字、比较是不是同一句时再用兼容分解。展示给读者的那一行不动。"""
    return unicodedata.normalize("NFKC", line or "")


def _scrub(line: str) -> str:
    line = re.sub(r"\s+", " ", (line or "").strip())
    return _SUFFIX.sub("", line).strip()


def _norm(line: str) -> str:
    return _PUNCT.sub("", _fold(line).lower().replace(" ", ""))


def _account_line(line: str) -> bool:
    """「方伟看十年」或带「作者」的账号栏。引用回复里用它把网友和方伟切开。"""
    return _ACCOUNT_BADGE.match(line) is not None or _ACCOUNT_NAME.match(line) is not None


def _wechat_signal(lines: list[str]) -> bool:
    if any(_GROUP_TITLE.match(line) for line in lines):
        return True
    if any(_account_line(line) for line in lines):
        return True
    return _WECHAT.search("\n".join(lines)) is not None


def _compact_host(line: str) -> str:
    return re.sub(r"\s+", "", (line or "").strip())


def _is_host_line(line: str) -> bool:
    return _HOST_LINE.fullmatch(_compact_host(line)) is not None


def _cjk_name(line: str, size: int) -> bool:
    chars = _CJK.findall(line or "")
    if len(chars) != size or len(chars) != len(line or ""):
        return False
    return not any(char in _STOP_CHAR for char in chars)


def _utterance(line: str) -> bool:
    return (line or "").strip() in _NOT_NAMES


def _content(line: str) -> bool:
    if not line or _is_noise(line):
        return False
    if _utterance(line):
        return True
    if re.search(r"[。！？!?，,、；;：:]", line):
        return True
    if len(_MEANING.findall(line)) >= 8:
        return True
    return bool(re.search(r"\d", line))


def _is_price(line: str) -> bool:
    return _PRICE.fullmatch(_fold(line or "")) is not None


def _is_time(line: str) -> bool:
    return _TIME.fullmatch(_fold(line or "")) is not None


def _is_store(line: str) -> bool:
    return _STORE.fullmatch(line or "") is not None


def _is_commerce(line: str) -> bool:
    return _COMMERCE.search(line or "") is not None


def _is_app_fragment(line: str) -> bool:
    return _APP_FRAGMENT.match(line or "") is not None


def _is_card_anchor(line: str) -> bool:
    folded = _fold(line or "")
    return bool(
        _is_price(line)
        or _CHROME_WORD.fullmatch(folded)
        or _is_store(line)
        or _is_commerce(line)
    )


def _latin_name(line: str) -> bool:
    text = (line or "").strip()
    if not _LATIN_NAME.fullmatch(text):
        return False
    if text.lower() in _LATIN_STOP or _is_host_line(_compact_host(text)):
        return False
    return True


def _name_shape(line: str) -> bool:
    return _cjk_name(line, 2) or _cjk_name(line, 3) or _latin_name(line)


def _is_sku(line: str) -> bool:
    """夹在小程序、价格、店铺之间的商品名，例如「黑糖珍珠牛乳」「热」。"""
    if not line or _is_noise(line) or _is_time(line) or _is_card_anchor(line):
        return False
    if _is_host_line(_compact_host(line)) or _name_shape(line) or _utterance(line):
        return False
    if re.search(r"[。！？!?，,、；;：:呢吗]", line):
        return False
    return len(line) <= 12


def _speaker_body(line: str) -> str:
    """去掉「方伟：/网友：」，看剩下是不是界面字。"""
    plain = _plain(line or "").strip()
    match = _STRUCTURED.match(plain)
    if match:
        return (match.group(2) or "").strip()
    return plain


def _is_ui_chrome(line: str) -> bool:
    """整行是界面字，或说话人后面只剩界面字。这种行不能标成方伟/网友。"""
    body = _fold(_speaker_body(line))
    return bool(body) and _CHROME_WORD.fullmatch(body) is not None


def _mixed_nickname(name: str) -> bool:
    """Aaron阿远 这种中英粘在一起的群昵称。不是句子。"""
    text = (name or "").strip()
    if not text or len(text) > 16 or re.search(r"[。！？!?，,、；;]", text):
        return False
    return bool(re.search(r"[A-Za-z]", text) and _CJK.search(text))


def _is_noise(line: str) -> bool:
    folded = _fold(line)
    if not folded or _SYMBOL.fullmatch(folded):
        return True
    # 截图边缘的单个假名。折行剩下的「的」是汉字，留着。
    if len(folded) == 1 and not _CJK.search(folded) and not folded.isalnum():
        return True
    if _is_time(line) or _STATUS.fullmatch(folded) or _CHROME_WORD.fullmatch(folded) or _is_ui_chrome(line):
        return True
    if _is_feed_chrome(line):
        return True
    if _GROUP_TITLE.fullmatch(folded) or _is_price(line) or _SYSTEM.search(folded):
        return True
    if _account_line(line):
        return True
    if _is_store(line) or _is_commerce(line) or _is_app_fragment(line):
        return True
    # 截图角落的假名。汉字单字（折出来的「的」）不是这种。
    if re.fullmatch(r"[\u3040-\u30ff]+", folded):
        return True
    # 「の 可按住转文字」一类：假名开头且几乎没有汉字。界面字本身由 _is_ui_chrome 认。
    if folded[:1] and "\u3040" <= folded[0] <= "\u30ff" and len(_CJK.findall(folded)) == 0:
        return True
    # 模糊 UI 认成的短全大写乱码，例如 HYIIV（元音种类少）。
    if re.fullmatch(r"[A-Z]{4,8}", folded) and len({ch for ch in folded if ch in "AEIOU"}) <= 1:
        return True
    return False


def _maybe_peel(line: str) -> str:
    peeled = _TRAIL_TIME.sub("", line).strip()
    if not peeled or peeled == line:
        return line
    compact = _compact_host(peeled)
    if _is_host_line(compact) or _cjk_name(peeled, 2) or _cjk_name(peeled, 3):
        return peeled
    return line


def _peel_handle(line: str) -> str:
    """李小红@think 这种昵称，@ 后面是备注，说话人只认前面的名字。"""
    peeled = _AT_SUFFIX.sub("", line or "").strip()
    if not peeled or peeled == line:
        return line
    if _is_host_line(_compact_host(peeled)) or _name_shape(peeled):
        return peeled
    return line


def _prepare_lines(raw_lines: list[str]) -> list[str]:
    own = [line.startswith(SIDE_SELF) for line in raw_lines]
    peeled = [_peel_handle(_maybe_peel(_plain(line))) for line in raw_lines]
    had_account = any(_account_line(line) for line in peeled)
    drop = {index for index, line in enumerate(peeled) if _is_card_anchor(line)}
    changed = True
    while changed:
        changed = False
        for index, line in enumerate(peeled):
            if index in drop or not _is_sku(line):
                continue
            if any(neighbor in drop for neighbor in (index - 1, index + 1)):
                drop.add(index)
                changed = True
    kept: list[str] = []
    placed_split = False
    for index, line in enumerate(peeled):
        if line == _GUEST_TURN:
            kept.append(_GUEST_TURN)
            continue
        if line == _HOST_TURN:
            kept.append(_HOST_TURN)
            continue
        if _is_time(line):
            kept.append(_BREAK)
            continue
        # 第一条账号栏留下分界；后面的「方伟看十年」是页脚，整行丢掉。
        # 必须写在 _is_noise 前面，否则账号栏会被直接跳过，栏上的引用就没有分界。
        if _account_line(line):
            if not placed_split:
                kept.append(_ACCOUNT_SPLIT)
                placed_split = True
            continue
        if index in drop or _is_noise(line):
            continue
        if len(kept) <= 1 and _GROUP_BARE.fullmatch(line):
            continue
        kept.append((SIDE_SELF if own[index] else "") + line)
    if had_account:
        kept = _drop_trailing_feed_title(kept)
    return _split_after_host_mention(kept)


def _drop_trailing_feed_title(lines: list[str]) -> list[str]:
    """公众号卡片末尾常粘上下一帖标题，例如「什么才叫做…的感觉？」。

    OCR 常截断成「什么才叫做」：没有问号也要丢掉。短、无句号、不像正文续写的尾行同理。
    """
    if not lines:
        return lines
    last = _plain(lines[-1])
    if last == _BREAK:
        return lines
    if _FEED_TITLE.match(last):
        return lines[:-1]
    # 截断标题：很短、没有句末标点、上一行已经收了句。
    if (
        4 <= len(last) <= 16
        and not re.search(r"[。！？!?…]$", last)
        and "，" not in last
        and lines[:-1]
        and re.search(r"[。！？!?…]$", _plain(lines[-2]))
    ):
        return lines[:-1]
    return lines


def _split_after_host_mention(lines: list[str]) -> list[str]:
    """@方伟 写在问句里时，后面另起的一句（如 Opus5.5…）另开一轮，不粘在问句上。

    只在 @方伟 之后、隔着不超过四个字的折行（「杭州」）时切开。没有这种形状就不动。
    """
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        out.append(line)
        if "@方伟" not in line or line == _BREAK:
            index += 1
            continue
        nxt = index + 1
        while nxt < len(lines) and lines[nxt] != _BREAK and re.fullmatch(r"[\u4e00-\u9fff]{1,4}", lines[nxt]):
            out.append(lines[nxt])
            nxt += 1
        if nxt < len(lines) and lines[nxt] != _BREAK and re.match(r"[A-Za-z0-9]", lines[nxt]):
            out.append(_BREAK)
        index = nxt
    return out


def _speaker_names(lines: list[str], signal: bool) -> set[str]:
    names: set[str] = set()
    pairs: list[str] = []
    seen_times: dict[str, int] = {}
    usable = [
        _plain(line)
        for line in lines
        if line not in (_BREAK, _ACCOUNT_SPLIT, _GUEST_TURN, _HOST_TURN) and not line.startswith(SIDE_SELF)
    ]
    for index, line in enumerate(usable):
        nxt = usable[index + 1] if index + 1 < len(usable) else ""
        if _cjk_name(line, 3) or _latin_name(line):
            seen_times[line] = seen_times.get(line, 0) + 1
            if _content(nxt):
                names.add(line)
        if signal and _cjk_name(line, 2) and not _utterance(line) and _content(nxt):
            pairs.append(line)
    for line, count in seen_times.items():
        # 同一个人开口不止一次时，短回复前的那一行也是名字，不能留在正文里。
        if count >= 2:
            names.add(line)
    if signal and len(pairs) >= 2:
        names.update(pairs)
    return names


def _guest_label(name: str) -> bool:
    name = name.strip()
    if not name or name in {HOST, GUEST} or _utterance(name) or name in _NOT_NAMES:
        return False
    if _is_host_line(_compact_host(name)):
        return False
    if _cjk_name(name, 2) or _cjk_name(name, 3) or _latin_name(name) or _mixed_nickname(name):
        return True
    chars = _CJK.findall(name)
    return 4 <= len(chars) <= 12 and len(chars) == len(name) and not any(char in _STOP_CHAR for char in chars)


def _classify(line: str, names: set[str]) -> tuple[str, str]:
    structured = _STRUCTURED.match(line)
    if structured:
        who = HOST if structured.group(1) == HOST else GUEST
        return who, (structured.group(2) or "").strip()
    compact = _compact_host(line)
    if _is_host_line(compact):
        return HOST, ""
    prefixed = _HOST_PREFIX.match(line)
    if prefixed:
        return HOST, prefixed.group(2).strip()
    inline = _INLINE.match(line)
    if inline:
        name, rest = inline.group(1).strip(), inline.group(2).strip()
        if _is_host_line(_compact_host(name)):
            return HOST, rest
        if rest and _guest_label(name):
            return GUEST, rest
    # 名字集合里的行永远是说话人，哪怕下一句很短（「王小明 / 数据确实是好的」）。
    if line in names:
        return GUEST, ""
    return "", ""


def _walk(lines: list[str], names: set[str], *, self_unnamed: bool) -> list[Turn]:
    turns: list[Turn] = []
    speaker = ""
    buf: list[str] = []
    # 有账号栏时，栏上整段是引用，不看绿/灰、也不把靠右的侧标记当成方伟。
    quote_reply = _ACCOUNT_SPLIT in lines
    past_account = False

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        if speaker:
            who = speaker
        elif quote_reply and not past_account:
            who = GUEST
        else:
            who = HOST if self_unnamed else GUEST
        turns.append(Turn(who, buf))
        buf = []

    for line in lines:
        if line == _ACCOUNT_SPLIT:
            flush()
            speaker = ""
            past_account = True
            continue
        if line == _GUEST_TURN:
            flush()
            speaker = GUEST
            continue
        if line == _HOST_TURN:
            flush()
            speaker = HOST
            continue
        if line == _BREAK:
            flush()
            speaker = ""
            continue
        own = line.startswith(SIDE_SELF)
        text = _plain(line)
        if _is_ui_chrome(text):
            continue
        if quote_reply and not past_account:
            if speaker != GUEST:
                flush()
                speaker = GUEST
            if text:
                buf.append(text)
            continue
        if own:
            # 靠右的绿气泡是作者自己，不看这一行像不像别人的名字。
            if speaker != HOST:
                flush()
                speaker = HOST
            if text:
                buf.append(text)
            continue
        who, rest = _classify(text, names)
        if who:
            flush()
            speaker = who
            if rest:
                buf.append(rest)
            continue
        if not speaker:
            # 没有坐标、也没有别人的昵称：群聊里这是绿色气泡。表格不会进到这里。
            speaker = HOST if self_unnamed else GUEST
        buf.append(text)
    flush()
    return turns

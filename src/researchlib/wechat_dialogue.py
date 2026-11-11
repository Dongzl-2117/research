"""行首说话人：「方伟：」「网友：」和截图上照抄的网名。

识图交回结构以后（``researchlib.ocr_units``），谁说的话由看得见图的模型标好，排成「说话人：正文」。
这里只剩认行首说话人的几个函数：正文和评论里行首的「昵称：」加粗（``render.mark_dialogue_speakers``）、
认星主的显示名（``zsxq_comments``），以及把别的来源截图里的「方伟：」换成那个来源的作者名（``relabel_host``）。

以前整理自由文字识图的那套规则（按 App、按案例认界面字、猜说话人、跨图去重）删了。全量重认以后
图都按结构认，模型偶尔交回的自由文字和以前存的旧识图都当成几段文字，照原样留着（``ocr_units.plain_units``）。

「方伟：」是库里、识图结果和检索块的固定写法，意思是截图里发帖人自己的绿气泡。方伟的来源就是方伟；
别的来源（乘风破浪、孤独进化体……）给读者看、交给模型时用 relabel_host 换成那个来源的作者名，库里不改。
"""

from __future__ import annotations

import re
import unicodedata

HOST = "方伟"

GUEST = "网友"

_CJK = re.compile(r"[一-鿿]")

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
        # 正文里常见的提示语、元信息词：冒号前面这一小段不是网名。
        "来源",
        "原文",
        "原标题",
        "标题",
        "作者",
        "出处",
        "链接",
        "备注",
        "结论",
        "原因",
        "方法",
        "问题",
        "答案",
        "建议",
        "数据",
        "图表",
        "正文",
        "摘要",
        "背景",
        "影响",
        "分析",
        "观点",
        "例子",
        "节选",
        "全文",
        "编者按",
        "时间",
        "地点",
        "事件",
        "人物",
        "发布",
        "更新",
        "风险",
        "风险提示",
        "注意事项",
        "免责声明",
        "数据来源",
        "原文链接",
        "核心观点",
        "投资建议",
        "重要提示",
        "特别提示",
        "内容摘要",
        "文章标题",
        "发布时间",
        "更新时间",
        "作者简介",
        # 推理小标题跟在空格后或单独折行时，也不是下一位说话人。
        "假设",
        "情景",
        "前提",
        "条件",
        "测算",
        "估算",
        "预测",
        "A股",
        "B股",
        "H股",
        "港股",
        "美股",
    }
)

# 显示名可以带状态后缀：方伟-最近在杭州、方伟Rey、方伟（星主）。句子里的「方伟说得对」不是名字。
# 这些词开头的一小段是正文（「以下转自茅台官方：…」），不是网名。
_NOT_NAME_HEAD = re.compile(
    r"^(?:以下|以上|如下|下面|上述|其中|此外|另外|同时|因此|所以|但是|不过|如果|因为|由于|"
    r"关于|对于|根据|按照|目前|现在|今天|昨天|明天|今年|去年|首先|其次|然后|最后|总之|"
    r"总结|结论|原因|方法|问题|答案|建议|注意|来源|原文|标题|作者|备注|说明|风险|数据|"
    r"转发|转自|摘自|引自|回复|引用)"
)

_HOST_LINE = re.compile(
    r"^(?:"
    r"方\s*伟(?:\s*Rey)?"
    r"(?:\s*[-－—]\s*[^\s：:，。！？!?]{1,16})?"
    r"(?:\s*[（(][^）)]{0,8}[）)])?"
    r"|Rey"
    r")$",
    re.I,
)

# 识图现在照抄截图上的网名（春风十里我自在：…）。标签只认冒号前的一小段，
# 不含空白、句读和斜杠；像不像人名交给 _guest_label 判，不堆名字对照表。
_NICK_LABEL = re.compile(
    r"^(?P<label>[^\s：:，。！？!?、；/\\]{1,20})\s*[:：]\s*(?P<body>.*)$", re.S
)

# 昵称后面粘着雪球/星球的身份徽章：「春风十里我自在作者」。
_AUTHOR_SUFFIX = re.compile(r"\s*[（(]?作者[）)]?$")

# 整理好的文字里行首的「方伟：」。relabel_host 换名字用。
_HOST_LABEL = re.compile(r"(?m)^(\s*)方伟\s*[:：]")

_LATIN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,19}$")

_LATIN_STOP = frozenset(
    {"gpt", "opus", "claude", "codex", "grok", "the", "and", "for", "you", "are", "not", "but", "http", "https", "www"}
)

# 网名后面的国旗、表情不参与「像不像人名」。正文里的表情不走这里。
_NICK_DECORATION = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # 区域指示符，两个组成一面国旗
    "\U0001F300-\U0001FAFF"
    "\u2600-\u27BF"
    "\uFE0F"
    "\u200D"
    "\u20E3"
    "]+"
)

# 指标按可组合的修饰词、量和度量识别，而不是逐个补「净资产收益率」等完整字段。
# 冒号只能证明它是标签；财务指标不能因此被赋予新的说话人身份。
_FINANCIAL_LABEL = re.compile(
    r"^(?:(?:19|20)\d{2}年|第?[一二三四1-4]季度|季度|年度|本期|上期|同期|期初|期末|"
    r"全年|当期|上年|去年|今年|每股|平均|合计|小计|调整后|调整前|扣非|归母|归属母公司|"
    r"税前|税后|营业|经营|研发|销售|管理|财务|资本|自由|总|净|毛)*"
    r"(?:营收|收入|利润|利率|利|现金流量?|资产|负债|权益|收益|成本|费用|开支|投入|资本|现金|"
    r"市值|市盈率|市净率|营运资金|同比|环比|增速|ROE|ROA|ROI|EPS|EBITDA|EBIT|CAPEX)"
    r"(?:(?:收益|回报|同比|环比|增长|变动|增减|下降)*(?:率|额|量|占比|增速|变化))?$",
    re.I,
)

# 新说话人标签需要额外依据：独立昵称行、昵称装饰，或常见姓名／称呼的形状。
# 这只是保守的边界线索，不把所有二至十二个汉字都当成人名。
_NAME_START = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许邓冯韩曹曾彭萧蔡潘田董袁于余叶蒋杜苏魏程吕丁沈"
    "任姚卢傅钟姜崔谭廖范汪陆金石戴贾韦夏邱方侯邹熊孟秦白江阎薛尹段雷黎史龙陶贺顾毛郝龚邵万钱严覃武"
    "戚孔汤池鲁季乔牛阿小老"
)

_NICK_IDENTITY_END = re.compile(r"(?:先生|女士|同学|老师|掌柜|居士|客|君)$|我.*(?:自在|逍遥|自由)$")

# 拉丁昵称没有汉字姓名形状：独立行首标签还须接明显的发言开头。
# 数值、单位或不能确认的文字不据此变成人名，保留原字并提示边界未核实。
_SPEECH_LEAD = re.compile(
    r"^(?:我(?![司国行方])|我们|你|他|她|咱们|同意|不同意|赞同|不赞同|谢谢|收到|明白|好的|是的|"
    r"(?:I|We|You|He|She|They|Yes|No|Thanks|Agreed)\b)", re.I,
)


def relabel_host(text: str, host: str = HOST) -> str:
    """别的来源的截图：行首的「方伟：」换成这个来源的作者名。方伟的来源原样返回。"""
    name = (host or "").strip()
    if not text or not name or name == HOST or name == GUEST:
        return text or ""
    return _HOST_LABEL.sub(lambda match: f"{match.group(1)}{name}：", text)


def _nick_core(name: str) -> str:
    """去掉网名上的国旗和表情，剩下的才拿来判断像不像人名。"""
    return _NICK_DECORATION.sub("", name or "").strip()


def _is_embedded_speaker(label: str, known: set[str]) -> bool:
    """行中间的冒号本身不证明换人，须有独立昵称或姓名形状等额外依据。"""
    cleaned = _clean_label(label)
    if not cleaned or '@' in cleaned or ('.' in cleaned and cleaned not in known):
        return False
    if cleaned in {HOST, GUEST} or _is_host_line(_compact_host(cleaned)):
        return True
    core = _nick_core(cleaned)
    if not core or not _guest_label(cleaned):
        return False
    if cleaned in known:
        return True
    if not _CJK.search(core):
        return False
    return (
        core != cleaned
        or (len(core) in (2, 3) and core[0] in _NAME_START)
        or (len(core) >= 4 and bool(_NICK_IDENTITY_END.search(core)))
        or (len(core) >= 4 and core[-2] in '阿小老' and core[-1] in _NAME_START)
    )


def _latin_speech_label(label: str, body: str) -> bool:
    """独立一行的拉丁昵称有发言语境才确认；不把未知英文指标名按字形直接当人。"""
    cleaned = _clean_label(label)
    return bool(
        _latin_name(_nick_core(cleaned)) and _guest_label(cleaned) and _SPEECH_LEAD.match((body or '').lstrip())
    )


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


def _latin_name(line: str) -> bool:
    text = (line or "").strip()
    if not _LATIN_NAME.fullmatch(text):
        normalized = unicodedata.normalize('NFC', text)
        def latin_letter(char: str) -> bool:
            return unicodedata.name(char, '').startswith('LATIN ')

        if not (
            3 <= len(normalized) <= 20 and latin_letter(normalized[0])
            and all(latin_letter(char) or char in '0123456789._-' for char in normalized)
        ):
            return False
    if text.lower() in _LATIN_STOP or _is_host_line(_compact_host(text)):
        return False
    return True


def _clean_label(label: str) -> str:
    """标签上的 @ 和身份徽章去掉：@春风十里我自在、春风十里我自在作者。"""
    text = re.sub(r"^@\s*", "", (label or "").strip())
    return _AUTHOR_SUFFIX.sub("", text).strip()


def split_speaker(line: str, *, names: bool = False) -> tuple[str, str] | None:
    """拆行首的说话人：返回 (标签, 正文)。

    默认只认「方伟」「网友」和方伟的显示名；names=True 再认识图照抄的截图网名
    （春风十里我自在：…）。冒号前的字像人名才认；「注意：」「10:38」「https:」不是说话人。
    """
    plain = (line or "").strip()
    match = _NICK_LABEL.match(plain)
    if not match:
        return None
    label = _clean_label(match.group("label"))
    if not label:
        return None
    body = (match.group("body") or "").strip()
    if label == HOST or label == GUEST:
        return label, body
    if _is_host_line(_compact_host(label)):
        return HOST, body
    if names and (_is_embedded_speaker(label, set()) or _latin_speech_label(label, body)):
        return label, body
    return None


def _mixed_nickname(name: str) -> bool:
    """Tony小李 这种中英粘在一起的群昵称。不是句子。"""
    text = (name or "").strip()
    if not text or len(text) > 16 or re.search(r"[。！？!?，,、；;]", text):
        return False
    return bool(re.search(r"[A-Za-z]", text) and _CJK.search(text))


def _guest_label(name: str) -> bool:
    name = name.strip()
    if not name or name in {HOST, GUEST} or _utterance(name) or name in _NOT_NAMES:
        return False
    if _is_host_line(_compact_host(name)):
        return False
    if _NOT_NAME_HEAD.match(name):
        return False
    core = _nick_core(name)
    if not core:
        return False
    if _FINANCIAL_LABEL.fullmatch(core):
        return False
    if core != name:
        if core in _NOT_NAMES or _utterance(core) or _NOT_NAME_HEAD.match(core):
            return False
        if _is_host_line(_compact_host(core)):
            return False
    if _cjk_name(core, 2) or _cjk_name(core, 3) or _latin_name(core) or _mixed_nickname(core):
        return True
    chars = _CJK.findall(core)
    # 网名可以带「我 / 的」这类字（春风十里我自在）；正文里的提示语已放进 _NOT_NAMES。
    return 4 <= len(chars) <= 12 and len(chars) == len(core)

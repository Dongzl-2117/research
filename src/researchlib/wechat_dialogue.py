"""把微信截图的 OCR 行整理成博客式对话。

微信里绿色气泡是作者自己（方伟），通常靠右、上面不写名字；灰色气泡是别人，通常靠左、上面有昵称。
RapidOCR 不保留颜色。有文字框时，`order_text` 给靠右的行加上不显示的侧标记；没有标记的旧识别结果，
群聊里不带昵称的气泡按绿色本人处理，带别人昵称的才是网友。不调用模型。

- 靠右（或没有坐标时、不带别人昵称）的气泡 →「方伟：」
- 别人的昵称（姜禄彦、徐哲航、Dylan，以及只写了名字的）→「网友：」，正文里不再留这个名字
- 显示名「方伟 / 方伟Rey / 方伟-最近在杭州」仍是方伟。句子里的 @方伟 不是说话人
- 方伟连续说的合成一块；网友每次开口仍分开，不因为都标成网友就并成一段
- 跨图重复的句子只留一次；后一张图只是把上一句又认了一遍时，新的半句接回上一块
- 群名、价格标签、小程序、设为群待办、状态栏时间电量这些界面字去掉

财报表格、贴成长文的图没有这些说话人或微信界面，就原样留下，不套对话。
知识星球引用回复不是微信气泡：第一条「方伟看十年 / 作者」账号栏是分界，栏上的灰字是「网友：」，
栏下的回复是「方伟：」。账号栏本身和卡片末尾的下一帖标题去掉。栏上没有正文、一上来就是账号栏的，
仍整段是方伟。电商商品图、带坐标轴的图表、数据表、Reddit 等外网评论截图不套对话，阅读页只留原图链接。
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
# 群名不一定带「群」字，例如「1633 看十年·线下深度交流(80)」。
_GROUP_TITLE = re.compile(
    r"^(?:\d{1,6}\s+)?[^\n（(]{0,24}群\s*[（(]\d{2,4}[）)]$"
    r"|^(?:\d{2,6}\s+).{2,40}[（(]\d{1,4}[）)]$"
)
_LATIN_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,19}$")
_LATIN_STOP = frozenset(
    {"gpt", "opus", "claude", "codex", "grok", "the", "and", "for", "you", "are", "not", "but"}
)
_GROUP_BARE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]{2,16}群$")
_TIME = re.compile(
    r"^(?:上午|下午|晚上|凌晨|昨天|今天|前天|星期[一二三四五六日天])?\s*\d{1,2}:\d{2}(?::\d{2})?[≥>≤<]*$"
    r"|^(?:昨天|今天|前天|星期[一二三四五六日天])$"
    r"|^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s+\d{1,2}:\d{2})?[≥>≤<]*$"
)
_STATUS = re.compile(
    r"^(?:微信|WeChat|返回|发送|按住\s*说话|以下是新消息|消息免打扰)$"
    r"|^\d{1,3}\s?%$"
    r"|^[45]G$"
    r"|^(?:LTE|Wi-?Fi|VPN|HD|NFC)$",
    re.I,
)
_CHROME_WORD = re.compile(r"^(?:设为群待办|设为待办|群待办|小+程序|进入小程序|查看详情|来自小程序)$")
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
_WECHAT = re.compile(r"微信|WeChat|设为群待办|设为待办|群待办|小+程序|按住\s*说话|以下是新消息|美团外卖")
# 朋友圈 / 公众号卡片上的账号栏。不是气泡里的话。
# 知识星球引用回复里，第一条这样的行把栏上的灰字和栏下的回复切开。
_ACCOUNT_BADGE = re.compile(r"^方伟看十年作者$|^看十年作者$")
_ACCOUNT_NAME = re.compile(r"^方伟看十年$|^看十年$")
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
# 时间行隔开两段话。文字丢掉，但要断开上一句，不能把下一位的话接上去。
_BREAK = "\x1e"
# 第一条账号栏。栏上已经收成网友，栏下改按方伟。本身不显示。
_ACCOUNT_SPLIT = "\x1f"
_TRAIL_TIME = re.compile(
    r"\s*(?:上午|下午|晚上|凌晨|昨天|今天|前天)?\s*\d{1,2}:\d{2}(?::\d{2})?\s*$"
)
_SUFFIX = re.compile(r"[\s,，]*?(?:设为群待办|设为待办|群待办)\s*$")
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
    return "\n\n".join(blocks)


def present_stored_ocr(stored: str) -> str:
    """库里可能还是按图拼接的原始识别，也可能已经是对话。两种再跑都得到对话。"""
    text = (stored or "").strip()
    if not text:
        return ""
    pieces = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return present_ocr_pieces(pieces or [text])


def speaker_note(text: str) -> str:
    """摘要和观点的提示里补一句说话人，没有对话标记时不加。"""
    if "方伟：" in (text or "") or "网友：" in (text or ""):
        return "群聊已整理成「方伟：」和「网友：」。方伟是星主，其余人都写成网友，不要把网友的话写成方伟的判断。"
    return ""


def _plain(line: str) -> str:
    return line[len(SIDE_SELF) :] if line.startswith(SIDE_SELF) else line


def parse_shot(text: str) -> Shot:
    original = (text or "").strip()
    if not original:
        return Shot("prose", [], "")
    raw_lines = [_scrub(line) for line in _normalize(original).splitlines()]
    raw_lines = [line for line in raw_lines if line]
    if not raw_lines:
        return Shot("prose", [], original)
    # 商品图、图表、外网评论截图、数据表先拦下：不能进微信对话。
    if (
        _is_product_shot(raw_lines)
        or _is_chart_shot(raw_lines)
        or _is_table_shot(raw_lines)
        or _is_foreign_comment_shot(raw_lines)
    ):
        return Shot("media", [], "")
    signal = _wechat_signal(raw_lines)
    lines = _prepare_lines(raw_lines)
    names = _speaker_names(lines, signal)
    # 已经是「方伟：」「网友：」的块再跑也要能合并，不能因为界面字已经被拿掉就退回原文。
    visible = [_plain(line) for line in lines if line not in (_BREAK, _ACCOUNT_SPLIT)]
    labeled = bool(visible) and _STRUCTURED.match(visible[0]) is not None
    hosted = any(_is_host_line(line) for line in visible) or any(line.startswith(SIDE_SELF) for line in lines)
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
        return Shot("prose", [], original)
    # 群聊里没有坐标时，不写别人昵称的气泡是作者自己的绿色气泡。有侧标记时以标记为准。
    turns = _walk(lines, names, self_unnamed=bool(signal or names or hosted))
    had_account = any(_account_line(_plain(line)) for line in raw_lines)
    family = "moments" if had_account else ("group" if signal else "chat")
    if turns:
        return Shot("dialogue", turns, "", family)
    # 整张都是界面字时不要把原文退回去，否则价格和小程序会漏进正文。
    if signal or names or hosted or labeled:
        return Shot("dialogue", [], "", family)
    return Shot("prose", [], original)


def _is_product_shot(lines: list[str]) -> bool:
    """亚马逊 / Temu 商品页：价格、Sponsored、购物车。不是群聊。"""
    joined = "\n".join(lines)
    hits = _PRODUCT_UI.findall(joined)
    if len(hits) >= 2:
        return True
    dollars = len(re.findall(r"(?<![A-Za-z])\$\s*\d", joined))
    stars = len(re.findall(r"★", joined))
    return dollars >= 3 and stars >= 2


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


def _is_noise(line: str) -> bool:
    folded = _fold(line)
    if not folded or _SYMBOL.fullmatch(folded):
        return True
    # 截图边缘的单个假名。折行剩下的「的」是汉字，留着。
    if len(folded) == 1 and not _CJK.search(folded) and not folded.isalnum():
        return True
    if _is_time(line) or _STATUS.fullmatch(folded) or _CHROME_WORD.fullmatch(folded):
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
    """徐哲航@think 这种昵称，@ 后面是备注，说话人只认前面的名字。"""
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
        if line not in (_BREAK, _ACCOUNT_SPLIT) and not line.startswith(SIDE_SELF)
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
    if _cjk_name(name, 2) or _cjk_name(name, 3) or _latin_name(name):
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
    # 名字集合里的行永远是说话人，哪怕下一句很短（「姜禄彦 / 数据确实是好的」）。
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
        if line == _BREAK:
            flush()
            speaker = ""
            continue
        own = line.startswith(SIDE_SELF)
        text = _plain(line)
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

"""把微信截图的 OCR 行整理成博客式对话。

识别结果存在 `image_texts.text`：RapidOCR 只留下从上到下的文字行，文字框的左右位置已经丢掉
（见 ocr.order_text）。所以这里不重新认图，也不调用模型。说话人规则是确定的，阅读页要当场算完：

- 这一行能对上星主（方伟 / 方伟Rey / Rey，含「方伟（星主）」）→「方伟：」
- 其他人（姜禄彦、徐哲航，以及只写了名字的）→「网友：」，正文里不再留这个名字
- 看不出是谁 →「网友：」，不因为语境猜成方伟
- 方伟连续说的合成一块；网友每次开口仍分开，不因为都标成网友就并成一段
- 跨图重复的句子只留一次；后一张图只是把上一句又认了一遍时，新的半句接回上一块
- 群名、价格标签、小程序、设为群待办、状态栏时间电量这些界面字去掉

财报表格、贴成长文的图没有这些说话人或微信界面，就原样留下，不套对话。
已经整理过的「方伟：」「网友：」再跑一遍还是同一份，方便写回 articles.ocr_text。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

HOST = "方伟"
GUEST = "网友"

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
_HOST_LINE = re.compile(
    r"^(?:方\s*伟(?:\s*Rey)?|方\s*伟\s*[（(]\s*星主\s*[）)]|Rey)$",
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
# 时间行隔开两段话。文字丢掉，但要断开上一句，不能把下一位的话接上去。
_BREAK = "\x1e"
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
    """一张图：对话轮次，或保持原样的正文（表格、长文）。"""

    kind: str
    turns: list[Turn]
    prose: str


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

    for piece in pieces:
        shot = parse_shot(piece)
        if shot.kind == "dialogue":
            pending.extend(shot.turns)
            continue
        flush()
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


def parse_shot(text: str) -> Shot:
    original = (text or "").strip()
    if not original:
        return Shot("prose", [], "")
    raw_lines = [_scrub(line) for line in _normalize(original).splitlines()]
    raw_lines = [line for line in raw_lines if line]
    if not raw_lines:
        return Shot("prose", [], original)
    signal = _wechat_signal(raw_lines)
    lines = _prepare_lines(raw_lines)
    names = _speaker_names(lines, signal)
    # 已经是「方伟：」「网友：」的块再跑也要能合并，不能因为界面字已经被拿掉就退回原文。
    labeled = bool(lines) and _STRUCTURED.match(lines[0]) is not None
    hosted = any(_is_host_line(line) for line in lines)
    if not lines or not (signal or names or labeled or hosted):
        return Shot("prose", [], original)
    turns = _walk(lines, names)
    if turns:
        return Shot("dialogue", turns, "")
    # 整张都是界面字时不要把原文退回去，否则价格和小程序会漏进正文。
    if signal or names or hosted or labeled:
        return Shot("dialogue", [], "")
    return Shot("prose", [], original)


def _same_sentence(left: str, right: str) -> bool:
    """两句去掉标点后相同，或其中一句包住另一句（滚动截图常把同一句再认一遍）。"""
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) < 8 or len(right) < 8:
        return False
    return left in right or right in left


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
            if norm == old or (len(norm) >= 8 and norm in old):
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
    if body and body[-1] not in "。！？!?…":
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


def _wechat_signal(lines: list[str]) -> bool:
    if any(_GROUP_TITLE.match(line) for line in lines):
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
    peeled = [_peel_handle(_maybe_peel(line)) for line in raw_lines]
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
    for index, line in enumerate(peeled):
        if _is_time(line):
            kept.append(_BREAK)
            continue
        if index in drop or _is_noise(line):
            continue
        if len(kept) <= 1 and _GROUP_BARE.fullmatch(line):
            continue
        kept.append(line)
    return kept


def _speaker_names(lines: list[str], signal: bool) -> set[str]:
    names: set[str] = set()
    pairs: list[str] = []
    seen_times: dict[str, int] = {}
    usable = [line for line in lines if line != _BREAK]
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


def _walk(lines: list[str], names: set[str]) -> list[Turn]:
    turns: list[Turn] = []
    speaker = ""
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        turns.append(Turn(speaker or GUEST, buf))
        buf = []

    for line in lines:
        if line == _BREAK:
            flush()
            speaker = ""
            continue
        who, rest = _classify(line, names)
        if who:
            flush()
            speaker = who
            if rest:
                buf.append(rest)
            continue
        if not speaker:
            speaker = GUEST
        buf.append(line)
    flush()
    return turns

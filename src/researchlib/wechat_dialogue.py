"""把微信截图的 OCR 行整理成博客式对话。

识别结果存在 `image_texts.text`：RapidOCR 只留下从上到下的文字行，文字框的左右位置已经丢掉
（见 ocr.order_text）。所以这里不重新认图，也不调用模型。说话人规则是确定的，阅读页要当场算完：

- 这一行能对上星主（方伟 / 方伟Rey / Rey，含「方伟（星主）」）→「方伟：」
- 其他人（姜禄彦、徐哲航，以及只写了名字的）→「网友：」，正文里不再留这个名字
- 看不出是谁 →「网友：」，不因为语境猜成方伟
- 连续同一个说话人合成一块
- 跨图重复的句子只留一次
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
    "的了着过和与或而且是不在把被让给到就也还很最这那你我他她它中但吧吗啊呢呀哦嗯又从对为以会能要说看想做"
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
_GROUP_TITLE = re.compile(r"^.{1,30}[（(]\d{2,4}[）)]$")
_GROUP_BARE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]{2,16}群$")
_TIME = re.compile(
    r"^(?:上午|下午|晚上|凌晨|昨天|今天|前天|星期[一二三四五六日天])?\s*\d{1,2}:\d{2}(?::\d{2})?$"
    r"|^(?:昨天|今天|前天|星期[一二三四五六日天])$"
    r"|^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s+\d{1,2}:\d{2})?$"
)
_STATUS = re.compile(
    r"^(?:微信|WeChat|返回|发送|按住\s*说话|以下是新消息|消息免打扰)$"
    r"|^\d{1,3}\s?%$"
    r"|^[45]G$"
    r"|^(?:LTE|Wi-?Fi|VPN|HD|NFC)$",
    re.I,
)
_CHROME_WORD = re.compile(r"^(?:设为群待办|设为待办|群待办|小程序|进入小程序|查看详情|来自小程序)$")
_PRICE = re.compile(r"^(?:券后价?\s*)?[¥￥$]\s*\d+(?:\.\d{1,2})?(?:\s*起)?$|^\d+\.\d{1,2}\s*元(?:起)?$")
_SYSTEM = re.compile(r"撤回了一条消息|拍了拍|以上是打招呼|你已添加")
_SYMBOL = re.compile(r"^[\s<>+=…\.。,，、·]+$")
_WECHAT = re.compile(r"微信|WeChat|设为群待办|设为待办|群待办|小程序|按住\s*说话|以下是新消息")
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
    if not turns:
        return Shot("prose", [], original)
    return Shot("dialogue", turns, "")


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
    """去重、合并连续同一说话人，写成「方伟：」「网友：」块。seen 跨图累计，就地更新。"""
    kept_norms = seen if seen is not None else []
    items: list[tuple[str, str]] = []
    for turn in turns:
        for line in turn.lines:
            cleaned = line.strip()
            if cleaned:
                items.append((turn.speaker, cleaned))
    norms = [_norm(line) for _speaker, line in items]
    keep = [len(norm) >= 2 for norm in norms]
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
    chosen: list[tuple[str, str]] = []
    for index, (speaker, line) in enumerate(items):
        if not keep[index]:
            continue
        norm = norms[index]
        if any(_same_sentence(norm, old) for old in kept_norms):
            continue
        kept_norms.append(norm)
        chosen.append((speaker, line))
    blocks: list[tuple[str, list[str]]] = []
    for speaker, line in chosen:
        if blocks and blocks[-1][0] == speaker:
            blocks[-1][1].append(line)
        else:
            blocks.append((speaker, [line]))
    return "\n\n".join(f"{speaker}：{_close_block(lines)}" for speaker, lines in blocks)


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


def _is_noise(line: str) -> bool:
    line = _fold(line)
    if not line or _SYMBOL.fullmatch(line):
        return True
    if _TIME.fullmatch(line) or _STATUS.fullmatch(line) or _CHROME_WORD.fullmatch(line):
        return True
    if _GROUP_TITLE.fullmatch(line) or _is_price(line) or _SYSTEM.search(line):
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


def _card_title(line: str) -> bool:
    if _is_host_line(_compact_host(line)) or _cjk_name(line, 2) or _cjk_name(line, 3):
        return False
    if re.search(r"[。！？!?，,、；;：:]", line):
        return False
    return 1 <= len(line) <= 12


def _prepare_lines(raw_lines: list[str]) -> list[str]:
    peeled = [_maybe_peel(line) for line in raw_lines]
    drop: set[int] = set()
    for index, line in enumerate(peeled):
        if _is_noise(line):
            drop.add(index)
    for index, line in enumerate(peeled):
        if "小程序" not in line and not _is_price(line):
            continue
        drop.add(index)
        for neighbor in (index - 1, index + 1):
            if neighbor < 0 or neighbor >= len(peeled) or neighbor in drop:
                continue
            if _card_title(peeled[neighbor]):
                drop.add(neighbor)
    kept: list[str] = []
    for index, line in enumerate(peeled):
        if index in drop:
            continue
        if len(kept) <= 1 and _GROUP_BARE.fullmatch(line):
            continue
        kept.append(line)
    return kept


def _speaker_names(lines: list[str], signal: bool) -> set[str]:
    names: set[str] = set()
    pairs: list[str] = []
    for index, line in enumerate(lines):
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if _cjk_name(line, 3) and _content(nxt):
            names.add(line)
        if signal and _cjk_name(line, 2) and not _utterance(line) and _content(nxt):
            pairs.append(line)
    if signal and len(pairs) >= 2:
        names.update(pairs)
    return names


def _guest_label(name: str) -> bool:
    name = name.strip()
    if not name or name in {HOST, GUEST} or _utterance(name) or name in _NOT_NAMES:
        return False
    if _is_host_line(_compact_host(name)):
        return False
    if _cjk_name(name, 2) or _cjk_name(name, 3):
        return True
    chars = _CJK.findall(name)
    return 4 <= len(chars) <= 12 and len(chars) == len(name) and not any(char in _STOP_CHAR for char in chars)


def _classify(line: str, nxt: str, names: set[str]) -> tuple[str, str]:
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
    if line in names and nxt and _content(nxt):
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

    for index, line in enumerate(lines):
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        who, rest = _classify(line, nxt, names)
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

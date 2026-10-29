"""识图误读的确定性校对。

不放进 ``textutil._OBVIOUS_TYPOS``。那张表是钉死的语病，出现就换，不看上下文。
这里只处理截图认错、而且上下文已经把词定死的两类，单独出现时不动：

- 「美」「墓」和「的」「地」形近。``墓地`` 单独出现（扫墓、公墓）不动；
  和家电品牌并列，或紧挨集团、电器、空调这类名字时，才当成公司名「美的」。
- ``free cash flow`` 的中间词只错一个字母（如 case）时改回 cash。
  别的 case、没有 flow 的 free case 不动。

阅读页 ``polish_display_text`` 和识图 ``normalize_vision_text`` 都走这里。
已经入库的 ``image_texts`` 不用改，部署后打开文章就能看见。
展示时不调 LLM：页面不能等模型，也不能为校对每次请求花钱。
同类判断写在 ``VISION_PROMPT`` 里，新识图时模型转写阶段就少写错；
模型还能按同样的判断处理这两类以外的滑字，这份校对只兜住已经入库的字。
"""

from __future__ import annotations

import re

# 并列连词。句号、逗号是分句，不算并列，避免「扫了墓地，格力涨了」被改掉。
_COORD = r"(?:[、/／]|以及|和|与|跟|及)"
_GAP = rf"\s*{_COORD}\s*"
# 和「美的」并列时几乎不会真的在说坟地。顺序长的在前，避免短名截断。
_PEER = r"小天鹅|方洪波|何享健|惠而浦|格力|海尔|库卡|KUKA|kuka"
_GRAVE_BEFORE = re.compile(rf"({_PEER})({_GAP})墓地")
_GRAVE_AFTER = re.compile(rf"墓地({_GAP})({_PEER})")
# 「墓地空调」「墓地集团」是公司/产品名，不是坟地。
_GRAVE_PRODUCT = re.compile(r"墓地(?=集团|电器|置业|小家电|空调|冰箱|洗衣机)")

# free + 一个词 + flow。中间词和 cash 只差一个字母才改。
# 不用 \b：Unicode 下汉字也算单词字符，「噪音了free」中间没有边界。
_FREE_FLOW = re.compile(
    r"(?<![A-Za-z])(free)(\s+)([A-Za-z]+)(\s+)(flow)(?![A-Za-z])",
    re.I,
)


def _one_letter_off(got: str, want: str) -> bool:
    if len(got) != len(want):
        return False
    return sum(left != right for left, right in zip(got, want, strict=True)) == 1


def _match_case(sample: str, word: str) -> str:
    if sample.isupper():
        return word.upper()
    if sample[:1].isupper() and sample[1:].islower():
        return word[:1].upper() + word[1:]
    return word


def _repair_free_cash_flow(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        middle = match.group(3)
        if middle.lower() == "cash" or not _one_letter_off(middle.lower(), "cash"):
            return match.group(0)
        fixed = _match_case(middle, "cash")
        return f"{match.group(1)}{match.group(2)}{fixed}{match.group(4)}{match.group(5)}"

    return _FREE_FLOW.sub(replace, text)


def _repair_midea_grave(text: str) -> str:
    if "墓地" not in text:
        return text
    text = _GRAVE_BEFORE.sub(r"\1\2美的", text)
    text = _GRAVE_AFTER.sub(r"美的\1\2", text)
    return _GRAVE_PRODUCT.sub("美的", text)


def repair_vision_misreads(text: str) -> str:
    """校对识图滑字。没有「墓地」也没有 flow 时原样返回。"""
    if not text:
        return text or ""
    if "墓地" not in text and "flow" not in text.lower():
        return text
    return _repair_free_cash_flow(_repair_midea_grave(text))

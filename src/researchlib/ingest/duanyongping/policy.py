"""段永平博客：公司名要落在论证上。列举、致谢、闲聊、顺带举例都不算。"""

from __future__ import annotations

import re

from researchlib.mentions import incidental_sentence

# 公司名拿掉之后，句子里还要有这些，才算在论证这家公司。
# 不用单独的「贵」：「贵州」里就有。用「不贵 / 贵不贵」。
_STRONG = re.compile(
    r"生意|商业模式|模式|护城河|估值|便宜|不贵|太贵|很贵|贵不贵|偏贵|"
    r"买入|卖出|拿着|持有|持仓|现金流|品牌|渠道|定价|回购|分红|股价|市值|"
    r"看懂|看不懂|壁垒|利润|收入|垄断|份额|安全边际|能力圈|企业文化|管理层|"
    r"财报|业绩|泡沫|下跌|上涨|增长|竞争|消费者|用户|产品"
)
_SOCIAL = re.compile(
    r"感谢|谢谢|致谢|多谢|感恩|恭喜|祝贺|吃饭|打球|高尔夫|旅游|早安|晚安|"
    r"生日快乐|哈哈|呵呵|聚会|喝茶|闲聊|周末愉快|聊天"
)
_LISTISH = re.compile(
    r"(?:[\u4e00-\u9fffA-Za-z0-9]{2,16}[、,，]){1,}[\u4e00-\u9fffA-Za-z0-9]{2,16}"
)
_FILLER = re.compile(
    r"[\s，,、。！？!?；;：:的了和与及以及或还有包括有是等之类这些那些"
    r"公司企业股票朋友博友网友大家啊呀呢吧吗哦（）()【】《》“”\"'—\-]+"
)


def without_names(sentence: str, names: list[str]) -> str:
    text = sentence or ""
    for name in sorted({item for item in names if item}, key=len, reverse=True):
        text = text.replace(name, " ")
    return text


def _compact(text: str) -> str:
    return _FILLER.sub("", text or "")


def sentence_role(sentence: str, surfaces: list[str]) -> str:
    """real：论证落在这些公司。incidental：举例。weak：列举、致谢、闲聊。none：没有公司名。"""
    names = [item for item in surfaces if item]
    if not names:
        return "none"
    if incidental_sentence(sentence):
        return "incidental"
    residue = without_names(sentence, names)
    social = bool(_SOCIAL.search(sentence))
    strong = bool(_STRONG.search(residue))
    if social and not strong:
        return "weak"
    if strong:
        return "real"
    if _LISTISH.search(sentence) or len(names) >= 2:
        return "weak"
    if "？" in sentence or "?" in sentence:
        return "real"
    if len(_compact(residue)) <= 4:
        return "weak"
    return "real"


def body_argues(body: str) -> bool:
    """标题就是公司名时，正文要真的在下判断，致谢和闲聊不够。"""
    text = body or ""
    if not text.strip():
        return False
    if _SOCIAL.search(text) and not _STRONG.search(text):
        return False
    return bool(_STRONG.search(text))


def title_is_only_companies(title: str, surfaces: list[str]) -> bool:
    names = [item for item in surfaces if item]
    if not names:
        return False
    return _compact(without_names(title or "", names)) == ""


def anchored_names(title: str, body: str) -> list[str]:
    """标题几乎只是公司名，正文在论证，公司名不必在正文里再出现一次。"""
    from researchlib.ingest.duanyongping.companies import matcher

    found, names = matcher()
    document = found.document(title or "", body or "")
    spans = document.spans(title or "")
    if not spans:
        return []
    surfaces = [(title or "")[start:end] for start, end, _company_id in spans]
    if not title_is_only_companies(title or "", surfaces) or not body_argues(body or ""):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for _start, _end, company_id in spans:
        name = names.get(company_id)
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered

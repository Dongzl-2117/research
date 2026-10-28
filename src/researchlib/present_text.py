"""阅读页展示层的轻量清洗：对话标签统一、明显错字。

只动展示，不改库里的原文。入库新写评论时也直接用「网友：」「方伟：」，
旧稿里的「读者问：」「作者答：」「作者补充：」打开页面时再映射过来。
明显错字（OCR / 笔误）用固定替换表，不改观点和口语。
"""

from __future__ import annotations

import re

# 行首说话人：旧稿三种 → 全文统一成「网友：」「方伟：」。
_LEGACY_SPEAKER = re.compile(
    r"(?m)^(读者问|作者答|作者补充)\s*[：:]\s*"
)
_SPEAKER_MAP = {
    "读者问": "网友：",
    "作者答": "方伟：",
    "作者补充": "方伟：",
}

# 明显错字 / 语病。只放不会误伤口语或观点的固定短语。
_TYPO_FIXES: tuple[tuple[str, str], ...] = (
    ("很新都东西", "很新的东西"),
    ("存在运用杠杆", "存在运营杠杆"),
)


def normalize_dialogue_labels(text: str) -> str:
    """把评论区旧标签收成「网友：」「方伟：」。正文里已有的同款标签不动。"""

    def replace(match: re.Match[str]) -> str:
        return _SPEAKER_MAP.get(match.group(1), match.group(0))

    return _LEGACY_SPEAKER.sub(replace, text or "")


def polish_display_text(text: str) -> str:
    """展示层改正明显错字。库内原文不动。"""
    out = text or ""
    for bad, good in _TYPO_FIXES:
        if bad in out:
            out = out.replace(bad, good)
    return out


def present_article_text(text: str) -> str:
    """正文 / OCR / 评论进阅读页前：先统一标签，再改错字。"""
    return polish_display_text(normalize_dialogue_labels(text or ""))

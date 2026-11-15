"""HTML 里的 ``&``：哪些是实体、哪些只是文字里的和号。

``HTMLParser(convert_charrefs=False)`` 把 ``AT&T``、``R&D`` 里的 ``&T``、``&D`` 也当成实体交给
``handle_entityref``，以前照样写回 ``&T;``，页面上显示成「AT&T;」（审核 T1）。认不得的名字写回 ``&amp;``，
后面的字照原样留着，分号不补。
"""

from __future__ import annotations

import re
from html.entities import html5, name2codepoint

# 「&名字;」里名字不是 HTML 实体的：模型交回的、以前的解析器补了分号的。
_NAMED = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
# 不是实体开头的裸 & （后面不是「名字;」「#数字;」「#x十六进制;」）。
_BARE_AMP = re.compile(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#[xX][0-9A-Fa-f]+);)")


def known_entity(name: str) -> bool:
    return name in name2codepoint or f"{name};" in html5


def entity_ref(name: str) -> str:
    """``handle_entityref`` 收到的名字写回成什么。认得的照写成实体，认不得的是文字里的 & 加几个字母。"""
    return f"&{name};" if known_entity(name) else f"&amp;{name}"


def escape_stray_ampersands(value: str) -> str:
    """裸 ``&`` 和认不得的「&名字;」写成 ``&amp;``，认得的实体、数字字符引用不动。

    认不得的「&名字;」：名字后面那个分号多半是解析器补的（AT&T 成了 AT&T;），去掉。
    原文里真写着「&名字;」的（很少见）由调用方另外判断，见 ``repair_entities``。
    """
    value = _BARE_AMP.sub("&amp;", value or "")
    return _NAMED.sub(lambda m: m.group(0) if known_entity(m.group(1)) else f"&amp;{m.group(1)}", value)


def repair_entities(target: str, source: str = "") -> str:
    """修已经入库的译文段：「&T;」这种认不得的实体改回 ``&amp;T``。

    原文这一段里就写着「&T;」（存成 ``&amp;T;``）的，分号是原文的，留着写成 ``&amp;T;``。
    """
    literal = {m.group(1) for m in re.finditer(r"&amp;([A-Za-z][A-Za-z0-9]*);", source or "")}

    def fix(match: re.Match[str]) -> str:
        name = match.group(1)
        if known_entity(name):
            return match.group(0)
        return f"&amp;{name};" if name in literal else f"&amp;{name}"

    return _NAMED.sub(fix, target or "")


def has_broken_entity(value: str) -> bool:
    """有没有「&名字;」而名字不是 HTML 实体的。"""
    return any(not known_entity(m.group(1)) for m in _NAMED.finditer(value or ""))

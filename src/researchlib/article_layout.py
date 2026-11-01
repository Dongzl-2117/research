"""阅读页的结构修正；只改展示，不回写原文或编造缺失资料。"""

from __future__ import annotations

import html
import re

_HEADING = re.compile(r"<h([1-6])\b([^>]*)>(.*?)</h\1>", re.I | re.S)
_STYLE = re.compile(r'''\s+style\s*=\s*(["']).*?\1''', re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_MISSING_TABLE = re.compile(
    r"<p>(\s*[\[【（(]?内嵌(?:电子)?表格未取到[\]】）)]?\s*)</p>"
)


def readable_headings(document: str) -> str:
    """正文只留二级以下标题；超过 180 字的误识别标题恢复为段落，保留全部文字。"""
    def replace(match):
        level, attrs, inner = match.groups()
        plain = re.sub(r"\s+", " ", html.unescape(_TAG.sub("", inner))).strip()
        if len(plain) > 180:
            return f"<p{_STYLE.sub('', attrs)}>{inner}</p>"
        level = "2" if level == "1" else level
        return f"<h{level}{attrs}>{inner}</h{level}>"

    return _HEADING.sub(replace, document)


def mark_unavailable_tables(document: str) -> str:
    return _MISSING_TABLE.sub(
        r'<aside class="source-missing" role="note">\1：原导出未包含此表格，需补充原始资料。</aside>', document
    )

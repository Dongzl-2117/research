"""把原文 Markdown 或 HTML 渲染成页面。外部图片不外链。"""

from __future__ import annotations

import re
from urllib.parse import unquote

import markdown
from markupsafe import Markup

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_SRC_ATTR = re.compile(r"""\bsrc\s*=\s*(["'])(.*?)\1""", re.I)
_DROP_BLOCKS = re.compile(
    r"<\s*(script|style|iframe|object|embed|link|meta)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.I | re.S,
)
_DROP_OPEN = re.compile(r"<\s*(script|style|iframe|object|embed|link|meta)\b[^>]*>", re.I)
_ON_ATTR = re.compile(r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_SRCSET = re.compile(r"""\s+srcset\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_JS_URL = re.compile(r"""((?:href|src)\s*=\s*["'])\s*javascript:[^"']*""", re.I)


def image_destination(raw: str) -> str:
    text = raw.strip().strip("<>")
    if " " in text and not text.startswith("http"):
        text = text.split()[0]
    return unquote(text.strip())


def sanitize_html(html: str) -> str:
    cleaned = _DROP_BLOCKS.sub("", html)
    cleaned = _DROP_OPEN.sub("", cleaned)
    cleaned = _ON_ATTR.sub("", cleaned)
    cleaned = _SRCSET.sub("", cleaned)
    cleaned = _JS_URL.sub(r"\1#", cleaned)
    return cleaned


def _mapped(dest: str, mapping: dict[str, str]) -> str | None:
    if dest in mapping:
        return mapping[dest]
    decoded = unquote(dest)
    if decoded in mapping:
        return mapping[decoded]
    return None


def _replace_destination(dest: str, mapping: dict[str, str]) -> str:
    cleaned = image_destination(dest)
    if cleaned.startswith(("http://", "https://", "//")):
        return ""
    mapped = _mapped(cleaned, mapping)
    if mapped:
        return mapped
    name = cleaned.rsplit("/", 1)[-1]
    return _mapped(name, mapping) or ""


def rewrite_markdown_images(body: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        target = _replace_destination(match.group(2), mapping)
        if not target:
            label = image_destination(match.group(2)).rsplit("/", 1)[-1] or "图片"
            if image_destination(match.group(2)).startswith(("http://", "https://", "//")):
                return "*[外部图片已省略]*"
            return f"*[{label} 缺图]*"
        return f"![{alt}]({target})"

    return _MD_IMAGE.sub(replace, body)


def rewrite_html_images(html: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        src = _SRC_ATTR.search(match.group(0))
        if src is None:
            return ""
        target = _replace_destination(src.group(2), mapping)
        if not target:
            return ""
        return f'<img src="{target}" alt="">'

    return _IMG_TAG.sub(replace, html)


def render_body(body: str, mapping: dict[str, str], fmt: str | None = None) -> Markup:
    if fmt == "html":
        html = rewrite_html_images(body or "", mapping)
        return Markup(sanitize_html(html))
    rewritten = rewrite_html_images(rewrite_markdown_images(body or "", mapping), mapping)
    html = markdown.markdown(
        rewritten,
        extensions=["extra", "nl2br", "sane_lists"],
        output_format="html",
    )
    return Markup(sanitize_html(html))

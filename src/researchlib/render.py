"""把原文 Markdown 或 HTML 渲染成页面。外部图片不外链。"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import unquote

import markdown
from markupsafe import Markup

from researchlib.ocr import useful_image_text

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


_TAG_URL_ATTRS = re.compile(r"""\b(data-src|data-original|src)\s*=\s*(["'])(.*?)\2""", re.I)


def normalize_image_url(raw: str) -> str:
    """外链图片地址的统一写法：去掉标题和尖括号，反转义 HTML，补上 //开头的协议。不是 http(s) 的返回空串。"""
    text = html_lib.unescape((raw or "").strip().strip("<>").strip())
    if not text:
        return ""
    text = text.split()[0]
    if text.startswith("//"):
        text = "https:" + text
    if not text.lower().startswith(("http://", "https://")):
        return ""
    return text


def img_tag_url(tag: str) -> str:
    """<img> 的图片地址。公众号懒加载把真地址放在 data-src，src 只是占位图，所以先看 data-src。"""
    attrs = {name.lower(): value for name, _quote, value in _TAG_URL_ATTRS.findall(tag)}
    for name in ("data-src", "data-original", "src"):
        value = (attrs.get(name) or "").strip()
        if value and not value.lower().startswith("data:"):
            return value
    return ""


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
        # 外链图片只在已经下载到本地时显示（research-fetch-images），不直接外链。
        return mapping.get(normalize_image_url(dest), "")
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
        url = img_tag_url(match.group(0))
        if not url:
            return ""
        target = _replace_destination(url, mapping)
        if not target:
            return ""
        return f'<img src="{target}" alt="">'

    return _IMG_TAG.sub(replace, html)


# 存在本地的图片文件名就是内容哈希。给它们挂上 id="img-<哈希前 12 位>"，图片文字（OCR）的引用能跳到这张图。
_LOCAL_IMG = re.compile(r'<img\b(?![^>]*\bid=)([^>]*?)\bsrc="(/media/[^"]*?([0-9a-f]{64})\.[A-Za-z0-9]+)"', re.I)


def image_anchor(content_hash: str) -> str:
    return f"img-{content_hash[:12]}"


def _anchor_images(html: str) -> str:
    return _LOCAL_IMG.sub(lambda m: f'<img id="{image_anchor(m.group(3))}"{m.group(1)}src="{m.group(2)}"', html)


# 单独成行的界面字：状态栏时间、电量、「微信」。整行对上才拿掉，消息正文里出现这些词留着。
_CHROME_LINE = re.compile(
    r"^(?:"
    r"\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s+\d{1,2}:\d{2})?"
    r"|星期[一二三四五六日天]"
    r"|昨天|今天|前天"
    r"|微信|WeChat"
    r"|返回|发送"
    r"|按住\s*说话"
    r"|以下是新消息"
    r"|\d{1,3}\s?%"
    r"|[45]G|LTE|Wi-?Fi|VPN"
    r")$",
    re.I,
)
_P_ONLY_IMG = re.compile(r"<p\b[^>]*>\s*(<img\b[^>]*>)\s*</p>", re.I)
_IMG_ID = re.compile(r'\bid="(img-[0-9a-f]{12})"', re.I)
_IMG_SRC_HASH = re.compile(r'\bsrc="(/media/[^"]*?([0-9a-f]{64})\.[A-Za-z0-9]+)"', re.I)
_IMG_ALT = re.compile(r'\balt="([^"]*)"', re.I)


def tidy_ocr_text(text: str) -> str:
    """阅读页上的轻量整理：拿掉单独成行的界面字，保留换行。

    整理完不够读就退回原文，不编对话、不补字。
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    kept: list[str] = []
    blank = False
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            if kept and not blank:
                kept.append("")
                blank = True
            continue
        blank = False
        if _CHROME_LINE.match(stripped):
            continue
        kept.append(stripped)
    cleaned = "\n".join(kept).strip()
    if useful_image_text(cleaned):
        return cleaned
    return raw


def looks_like_chat(text: str) -> bool:
    """一行一句、多数很短：按对话排。长文、表格仍按原文换行，不硬套聊天气泡。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return False
    short = sum(1 for line in lines if len(line) <= 28)
    return short * 2 >= len(lines)


def _reading_inner(text: str) -> tuple[str, str]:
    shown = tidy_ocr_text(text)
    if looks_like_chat(shown):
        parts = [f"<p>{html_lib.escape(line.strip())}</p>" for line in shown.splitlines() if line.strip()]
        return "ocr-chat", "".join(parts)
    return "", html_lib.escape(shown)


def _ocr_figure(anchor: str, src: str, alt: str, text: str) -> str:
    kind, inner = _reading_inner(text)
    classes = "ocr-reading ocr-chat" if kind else "ocr-reading"
    safe_src = html_lib.escape(src, quote=True)
    safe_alt = html_lib.escape(alt, quote=True)
    safe_id = html_lib.escape(anchor, quote=True)
    return (
        f'<figure class="ocr-shot" id="{safe_id}">'
        f'<div class="{classes}">{inner}</div>'
        f'<details class="ocr-original"><summary>点击看原图</summary>'
        f'<img src="{safe_src}" alt="{safe_alt}">'
        f"</details></figure>"
    )


def _convert_img(tag: str, texts: dict[str, str]) -> str:
    id_match = _IMG_ID.search(tag)
    src_match = _IMG_SRC_HASH.search(tag)
    if not id_match or not src_match:
        return tag
    raw = texts.get(src_match.group(2)) or ""
    if not useful_image_text(raw):
        return tag
    alt_match = _IMG_ALT.search(tag)
    alt = alt_match.group(1) if alt_match else ""
    return _ocr_figure(id_match.group(1), src_match.group(1), alt, raw)


def inline_image_ocr(html: str, texts: dict[str, str]) -> str:
    """有可读识别结果的本地图：先显示文字，原图收在「点击看原图」里。

    texts 是 content_hash → image_texts.text。空的、太短的图保持原样。
    锚点 id 挪到文字块上，检索里的「看这张图」仍跳到这里。
    """
    if not html or not texts:
        return html or ""

    def convert_p(match: re.Match[str]) -> str:
        converted = _convert_img(match.group(1), texts)
        if converted == match.group(1):
            return match.group(0)
        return converted

    rewritten = _P_ONLY_IMG.sub(convert_p, html)
    return _IMG_TAG.sub(lambda match: _convert_img(match.group(0), texts), rewritten)


def render_body(body: str, mapping: dict[str, str], fmt: str | None = None) -> Markup:
    if fmt == "html":
        html = rewrite_html_images(body or "", mapping)
        return Markup(_anchor_images(sanitize_html(html)))
    rewritten = rewrite_html_images(rewrite_markdown_images(body or "", mapping), mapping)
    html = markdown.markdown(
        rewritten,
        extensions=["extra", "nl2br", "sane_lists"],
        output_format="html",
    )
    return Markup(_anchor_images(sanitize_html(html)))

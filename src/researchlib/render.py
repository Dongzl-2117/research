"""把原文 Markdown 或 HTML 渲染成页面。外部图片不外链。"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import unquote

import markdown
from markupsafe import Markup

from researchlib.ocr import useful_image_text
from researchlib.wechat_dialogue import parse_shot, render_turns

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


def _img_spec(tag: str) -> tuple[str, str, str, str] | None:
    id_match = _IMG_ID.search(tag)
    src_match = _IMG_SRC_HASH.search(tag)
    if not id_match or not src_match:
        return None
    alt_match = _IMG_ALT.search(tag)
    alt = alt_match.group(1) if alt_match else ""
    return id_match.group(1), src_match.group(1), alt, src_match.group(2)


def _image_spans(html: str) -> list[tuple[int, int, str]]:
    """按出现顺序找出图片。单独成段的 <p><img></p> 整段换掉，避免留下空段落。"""
    covered: list[tuple[int, int]] = []
    spans: list[tuple[int, int, str]] = []
    for match in _P_ONLY_IMG.finditer(html):
        spans.append((match.start(), match.end(), match.group(1)))
        covered.append((match.start(), match.end()))
    for match in _IMG_TAG.finditer(html):
        if any(start <= match.start() and match.end() <= end for start, end in covered):
            continue
        spans.append((match.start(), match.end(), match.group(0)))
    spans.sort()
    return spans


def _dialogue_html(text: str) -> str:
    parts: list[str] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        label, sep, rest = block.partition("：")
        if sep and label in {"方伟", "网友"}:
            body = html_lib.escape(rest.lstrip("\n"))
            parts.append(f'<p><span class="who">{label}：</span>{body}</p>')
        else:
            parts.append(f"<p>{html_lib.escape(block)}</p>")
    return "".join(parts)


def _original_links(items: list[dict]) -> str:
    """连续截图的原图收成一行文字链接，点开就是图片地址。"""
    links: list[str] = []
    for number, info in enumerate(items, start=1):
        safe_src = html_lib.escape(info["src"], quote=True)
        safe_id = html_lib.escape(info["anchor"], quote=True)
        links.append(
            f'<a class="ocr-link" id="{safe_id}" href="{safe_src}" target="_blank" rel="noopener">原图 {number}</a>'
        )
    return f'<p class="ocr-links">{"".join(links)}</p>'


def _chat_bundle(items: list[dict], dialogue: str) -> str:
    inner = _dialogue_html(dialogue)
    return (
        '<figure class="ocr-shot">'
        f'<div class="ocr-reading ocr-dialogue">{inner}</div>'
        f"{_original_links(items)}"
        "</figure>"
    )


def inline_image_ocr(html: str, texts: dict[str, str]) -> str:
    """有可读识别结果的本地图：群聊先显示整理后的对话，原图是一行「原图 1」链接。

    连续几张微信截图合成一段，重复的句子只出现一次，不再每张图留一个空架子。
    表格和长文仍按原文换行，原图收在「点击看原图」里。没认出字的图保持原来的图片。
    texts 是 content_hash → image_texts.text。锚点 id 在原图链接上，检索里的「看这张图」仍跳到这里。
    """
    if not html or not texts:
        return html or ""
    spans = _image_spans(html)
    if not spans:
        return html
    parsed: list[dict | None] = []
    for _start, _end, tag in spans:
        spec = _img_spec(tag)
        if spec is None:
            parsed.append(None)
            continue
        anchor, src, alt, digest = spec
        raw = texts.get(digest) or ""
        if not useful_image_text(raw):
            parsed.append(None)
            continue
        parsed.append(
            {"anchor": anchor, "src": src, "alt": alt, "raw": raw, "shot": parse_shot(raw)}
        )
    outputs: list[str | None] = [None] * len(spans)
    seen: list[str] = []
    index = 0
    while index < len(spans):
        item = parsed[index]
        if item is None:
            index += 1
            continue
        if item["shot"].kind != "dialogue":
            outputs[index] = _ocr_figure(item["anchor"], item["src"], item["alt"], item["raw"])
            index += 1
            continue
        group = [index]
        nxt = index + 1
        while nxt < len(spans) and parsed[nxt] is not None and parsed[nxt]["shot"].kind == "dialogue":
            group.append(nxt)
            nxt += 1
        turns = [turn for cursor in group for turn in parsed[cursor]["shot"].turns]
        dialogue = render_turns(turns, seen).strip()
        if not dialogue:
            for cursor in group:
                info = parsed[cursor]
                outputs[cursor] = _ocr_figure(info["anchor"], info["src"], info["alt"], info["raw"])
        else:
            outputs[group[0]] = _chat_bundle([parsed[cursor] for cursor in group], dialogue)
            for cursor in group[1:]:
                outputs[cursor] = ""
        index = nxt
    result = html
    for (start, end, _tag), output in zip(reversed(spans), reversed(outputs), strict=True):
        if output is not None:
            result = result[:start] + output + result[end:]
    return result


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

"""公众号文章页（或极致了返回的正文 HTML）转成归档用的 Markdown。

原文页 mp.weixin.qq.com/s?… 的正文在 ``<div id="js_content">`` 里：一层层 section、span 包着 p，
图片是懒加载的 ``<img data-src=…>``。这里按出现顺序取文字和图片：段落、标题、列表项、引用、表格行各成一段，
``<br>`` 也断成两段；加粗留成 ``**…**``，链接留成 ``[文字](地址)``；脚本、样式、音视频卡片不要。
图片留公众号图床的地址，入库后由 ``research-fetch-images`` 下载到本地。
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

_BLOCK = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "center",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ul",
        "ol",
        "blockquote",
        "figure",
        "figcaption",
        "table",
        "tr",
        "pre",
        "dl",
        "dt",
        "dd",
        "hr",
    }
)
# 这些标签连里面的东西一起不要：脚本、样式，和公众号的音频、视频、小程序卡片。
_SKIP = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe", "mpvoice", "mpvideo", "qqmusic", "mp-miniprogram"}
)
_VOID = frozenset(
    {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
)
_HEADING = {"h1": "## ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "#### ", "h6": "#### "}
_INVISIBLE = re.compile(r"[​-‍⁠﻿]")
_SPACES = re.compile(r"[ \t\r\f\v 　]+")
_EMPTY_BOLD = re.compile(r"\*\*(\s*)\*\*")
_BOLD = re.compile(r"\*\*(\s*)(.+?)(\s*)\*\*")
# 源码里标签之间的换行缩进会在两个汉字中间留下空格，去掉。
_CJK = r"[　-〿一-鿿＀-￯]"
_CJK_GAP = re.compile(rf"(?<={_CJK}) (?={_CJK})")
# <br> 记成这个字符，和源码里的换行（当空格）分开。
_BREAK = "\x00"
_HIDDEN = re.compile(r"display\s*:\s*none", re.I)
# 原文页上正文后面的区块。万一正文里有标签没闭合、层数对不上，读到这些也停下，不把二维码、推荐读进来。
_AFTER_CONTENT = frozenset(
    {"js_pc_qr_code", "content_bottom_area", "js_article_bottom_bar", "js_bottom_ad_area", "js_sponsor_ad_area"}
)

# 原文页上没有正文时，页面上的说法 → 原因。
_UNAVAILABLE = (
    ("该内容已被发布者删除", "原文已删除"),
    ("此内容因违规无法查看", "原文因违规无法查看"),
    ("此内容被多人投诉", "原文被投诉，无法查看"),
    ("该公众号已迁移", "公众号已迁移"),
    ("环境异常", "微信要求验证（环境异常）"),
    ("完成验证后即可继续访问", "微信要求验证"),
)


class _Converter(HTMLParser):
    def __init__(self, root_id: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.root_id = root_id
        self.active = root_id is None
        self.found = root_id is None
        self.depth = 0
        self.done = False
        self.skip = 0
        self.blocks: list[str] = []
        self.parts: list[str] = []
        self.heading = ""
        self.lists: list[list] = []
        self.quote = 0
        self.links: list[tuple[int, str]] = []
        self.images: list[str] = []
        self.cells = 0

    # 标签 ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        values = {key: (value or "") for key, value in attrs}
        if not self.active:
            if self.root_id and values.get("id") == self.root_id:
                self.active = True
                self.found = True
                self.depth = 0 if tag in _VOID else 1
            return
        if self.root_id and values.get("id") in _AFTER_CONTENT:
            self._flush()
            self.done = True
            return
        if self.root_id and tag not in _VOID:
            self.depth += 1
        if self.skip:
            if tag not in _VOID:
                self.skip += 1
            return
        if tag in _SKIP or (tag not in _VOID and _HIDDEN.search(values.get("style", ""))):
            self.skip = 1
            return
        if tag == "br":
            self.parts.append(_BREAK)
        elif tag == "img":
            self._image(values)
        elif tag in _BLOCK:
            self._flush()
            if tag in _HEADING:
                self.heading = _HEADING[tag]
            elif tag in ("ul", "ol"):
                self.lists.append([tag, 0])
            elif tag == "li" and self.lists:
                self.lists[-1][1] += 1
            elif tag == "blockquote":
                self.quote += 1
            elif tag == "tr":
                self.cells = 0
        elif tag in ("td", "th"):
            if self.cells:
                self.parts.append(" | ")
            self.cells += 1
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag == "a":
            self.links.append((len(self.parts), values.get("href", "")))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/>、<img …/>：没有结束标签，不算进层数。
        if tag in _VOID:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.done or not self.active:
            return
        if self.root_id and tag not in _VOID:
            self.depth -= 1
            if self.depth <= 0:
                self._flush()
                self.done = True
                return
        if self.skip:
            if tag not in _VOID:
                self.skip -= 1
            return
        if tag in ("strong", "b"):
            self.parts.append("**")
        elif tag == "a":
            self._close_link()
        elif tag in _BLOCK:
            self._flush()
            if tag in _HEADING:
                self.heading = ""
            elif tag in ("ul", "ol") and self.lists:
                self.lists.pop()
            elif tag == "blockquote" and self.quote:
                self.quote -= 1

    def handle_data(self, data: str) -> None:
        if not self.active or self.done or self.skip:
            return
        if "\n" in data and not data.strip():
            # 标签之间排版用的换行缩进，不是文字里的空格。
            return
        self.parts.append(data.replace("\r", " ").replace("\n", " "))

    # 内容 ------------------------------------------------------------------

    def _image(self, values: dict[str, str]) -> None:
        url = (values.get("data-src") or values.get("src") or "").strip()
        if not url or url.startswith("data:"):
            return
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://mmbiz."):
            url = "https://" + url[len("http://") :]
        if not url.startswith(("http://", "https://")):
            return
        self._flush()
        self.images.append(url)
        self.blocks.append(f"{'> ' * self.quote}![]({url})")

    def _close_link(self) -> None:
        if not self.links:
            return
        start, href = self.links.pop()
        text = _clean_line("".join(self.parts[start:]))
        href = href.strip()
        if (
            text
            and href.startswith(("http://", "https://"))
            and _BREAK not in "".join(self.parts[start:])
            and "](" not in text
            and text != href
        ):
            self.parts[start:] = [f"[{text.replace('[', '［').replace(']', '］')}]({href})"]

    def _flush(self) -> None:
        if not self.parts:
            return
        text = "".join(self.parts)
        self.parts = []
        self.links = []
        lines = [_clean_line(line) for line in text.split(_BREAK)]
        lines = [line for line in lines if line]
        if not lines:
            return
        prefix = "> " * self.quote
        for index, line in enumerate(lines):
            if self.heading:
                line = self.heading + line.replace("**", "")
            elif index == 0 and self.lists:
                kind, count = self.lists[-1]
                line = (f"{count or 1}. " if kind == "ol" else "- ") + line
            self.blocks.append(prefix + line)

    def finish(self) -> tuple[str, list[str]]:
        self.close()
        self._flush()
        blocks: list[str] = []
        for block in self.blocks:
            if blocks and blocks[-1] == block and block.startswith("!["):
                continue
            blocks.append(block)
        return "\n\n".join(blocks).strip(), self.images


def _clean_line(text: str) -> str:
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text).strip()
    text = _CJK_GAP.sub("", text)
    text = text.replace("****", "")
    text = _EMPTY_BOLD.sub(lambda match: match.group(1), text)
    if text.count("**") % 2:
        # 加粗跨了段落：这一段的开头或结尾只剩半个标记，整段不加粗。
        text = text.replace("**", "")
    text = _BOLD.sub(lambda match: f"{match.group(1)}**{match.group(2)}**{match.group(3)}", text)
    text = text.strip()
    if text.strip("*").strip() == "":
        return ""
    return text


def html_to_markdown(html: str, *, root_id: str | None = None) -> tuple[str, list[str]]:
    """HTML → (Markdown, 按顺序的图片地址)。给了 root_id 时只转那个元素里面的部分。"""
    parser = _Converter(root_id)
    parser.feed(html or "")
    markdown, images = parser.finish()
    if root_id and not parser.found:
        return "", []
    return markdown, images


def page_markdown(page_html: str) -> tuple[str, list[str]]:
    """原文页的正文（#js_content）。没有正文时返回空串。"""
    return html_to_markdown(page_html, root_id="js_content")


def dajiala_markdown(content: str) -> tuple[str, list[str]]:
    """极致了 article_detail（mode=1）的正文。有的类型把换行写成字面上的反斜杠 n。"""
    text = (content or "").replace("\\r\\n", "<br>").replace("\\n", "<br>")
    return html_to_markdown(text)


def unavailable_reason(page_html: str) -> str:
    """原文页上没有正文时的原因（删除、违规、要验证）；认不出就说「原文页没有正文」。"""
    for marker, reason in _UNAVAILABLE:
        if marker in (page_html or ""):
            return reason
    return "原文页没有正文"


_META_TITLE = re.compile(r"""<meta\s+property=["']og:title["']\s+content=["']([^"']*)["']""", re.I)


def page_title(page_html: str) -> str:
    match = _META_TITLE.search(page_html or "")
    return unescape(match.group(1)).strip() if match else ""

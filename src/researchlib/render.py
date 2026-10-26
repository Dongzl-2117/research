"""把原文 Markdown 或 HTML 渲染成页面。外部图片不外链。"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import unquote

import markdown
from markupsafe import Markup

from researchlib.ocr import useful_image_text
from researchlib.textutil import strip_alert_markers
from researchlib.wechat_dialogue import parse_shot, render_turns
from researchlib.zsxq_comments import assemble_reader_comments, strip_breaks_before_comments

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_SRC_ATTR = re.compile(r"""\bsrc\s*=\s*(["'])(.*?)\1""", re.I)

# 正文 HTML 按白名单重写，见 sanitize_html。
_DROP_WITH_CONTENT = frozenset({
    "script", "style", "template", "iframe", "frame", "frameset", "object", "embed", "applet",
    "noscript", "noembed", "noframes", "svg", "math", "textarea", "select", "title", "head", "xmp",
})
_VOID = frozenset({"br", "hr", "img", "wbr", "col"})
_ALLOWED_TAGS = frozenset({
    "a", "abbr", "article", "aside", "b", "bdi", "bdo", "blockquote", "br", "caption", "center",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i",
    "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "rp", "rt", "ruby", "s", "samp",
    "section", "small", "span", "strike", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "time", "tr", "u", "ul", "var", "wbr",
})
_GLOBAL_ATTRS = frozenset({"id", "class", "title", "lang", "dir", "style", "align"})
_TAG_ATTRS = {
    "a": frozenset({"href", "name"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "table": frozenset({"width", "border", "cellpadding", "cellspacing"}),
    "td": frozenset({"colspan", "rowspan", "valign", "width"}),
    "th": frozenset({"colspan", "rowspan", "valign", "width", "scope"}),
    "col": frozenset({"span", "width"}),
    "colgroup": frozenset({"span", "width"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "ul": frozenset({"type"}),
    "li": frozenset({"value"}),
    "time": frozenset({"datetime"}),
    "details": frozenset({"open"}),
}
_BOOLEAN_ATTRS = frozenset({"open", "reversed"})
# 浏览器认协议前会删掉空白和控制字符，判断前也删掉。
_URL_NOISE = re.compile(r"[\x00-\x20\x7f]+")
_URL_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):", re.I)
_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
# 内联样式只去掉会加载外部资源、能写转义或盖住页面的声明，排版类的照留。
_STYLE_PROPERTY = re.compile(r"^-?[a-z][a-z-]*$")
_STYLE_UNSAFE = re.compile(
    r"url\s*\(|image(?:-set)?\s*\(|cross-fade\s*\(|element\s*\(|src\s*\(|expression\s*\("
    r"|javascript:|@import|behavior|binding|\\|/\*",
    re.I,
)


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


def _attr_value(value: str) -> str:
    return html_lib.escape(value, quote=False).replace('"', "&quot;")


def _safe_link(value: str) -> bool:
    scheme = _URL_SCHEME.match(_URL_NOISE.sub("", value))
    return scheme is None or scheme.group(1).lower() in _LINK_SCHEMES


def _local_path(value: str) -> bool:
    """外部图片不外链：图片只认站内路径（rewrite_*_images 已经换成 /media/...）。"""
    path = value.strip()
    return path.startswith("/") and not path.startswith("//") and "\\" not in path


def _clean_style(value: str) -> str:
    kept: list[str] = []
    for declaration in value.split(";"):
        name, sep, setting = declaration.partition(":")
        name, setting = name.strip().lower(), setting.strip()
        if not sep or not setting or not _STYLE_PROPERTY.match(name) or _STYLE_UNSAFE.search(setting):
            continue
        if name == "position" and setting.lower() not in {"static", "relative"}:
            continue
        kept.append(f"{name}: {setting}")
    return "; ".join(kept)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.skip_tag = ""
        self.skip_depth = 0

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        allowed = _TAG_ATTRS.get(tag, frozenset())
        parts: list[str] = []
        has_src = False
        for name, value in attrs:
            if name not in _GLOBAL_ATTRS and name not in allowed:
                continue
            if value is None:
                if name in _BOOLEAN_ATTRS:
                    parts.append(f" {name}")
                continue
            if name == "href" and not _safe_link(value):
                continue
            if name == "src":
                if not _local_path(value):
                    continue
                has_src = True
            if name == "style":
                value = _clean_style(value)
                if not value:
                    continue
            parts.append(f' {name}="{_attr_value(value)}"')
        if tag == "img" and not has_src:
            return None
        return "".join(parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        rendered = self._attrs(tag, attrs)
        if rendered is None:
            return
        self.out.append(f"<{tag}{rendered}>")
        if tag not in _VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_depth or tag in _DROP_WITH_CONTENT:
            return
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.open and self.open[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        # 只关自己开过的标签：多出来的 </div> 会关掉页面外层的容器。
        if tag not in self.open:
            return
        while self.open:
            name = self.open.pop()
            self.out.append(f"</{name}>")
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.out.append(html_lib.escape(data, quote=False))

    def result(self) -> str:
        self.close()
        self.out.extend(f"</{name}>" for name in reversed(self.open))
        self.open = []
        return "".join(self.out)


def sanitize_html(html: str) -> str:
    """按白名单重写正文 HTML：解析成标签和文字再写出来，文字一律转义。

    只留排版用的标签和属性，链接只认 http、https、mailto 和站内地址，图片只认站内路径。
    注释、脚本、内嵌页面、SVG 连内容一起丢；不认识的标签只丢标签、留文字。
    """
    parser = _Sanitizer()
    parser.feed(html or "")
    return parser.result()


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
    return (
        f'<figure class="ocr-shot">'
        f'<div class="{classes}">{inner}</div>'
        f"{_original_links([{'anchor': anchor, 'src': src}])}"
        "</figure>"
    )


def _media_bundle(items: list[dict]) -> str:
    """商品图、图表：不展示 OCR 乱码，只留「原图 N」。"""
    return f'<figure class="ocr-shot">{_original_links(items)}</figure>'



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


# 微信气泡一行大约 15 个汉字。顶到边的是折行，短行多半是这句说完了。
_OCR_WRAP_WIDTH = 14.5
_OCR_SENTENCE_END = re.compile(r"(?:[。！？!?…]|\.\.\.)$")
_CJK_CHAR = re.compile(r"[一-鿿]")


def _visual_width(text: str) -> float:
    """汉字算 1，英文和数字算半个，用来看这一行有没有顶到气泡边缘。"""
    return sum(1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else 0.5 for char in text)


def _flow_ocr_lines(text: str) -> str:
    """把截图硬换行收成段。``国\n产agents`` 接成「国产agents」，说话人仍按块分开。

    满行直接接上，避免半个词换行。短行后面留一个空格，两句没有句号时不粘成一个词。
    上一行已经收了句，下一行另起一段，避免星球长聊糊成一大段。
    旁路正文仍保留原来的换行，这里只影响阅读页。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    previous = lines[0]
    merged = previous
    for line in lines[1:]:
        if _OCR_SENTENCE_END.search(previous):
            merged += "\n\n" + line
        elif _visual_width(previous) >= _OCR_WRAP_WIDTH:
            merged += line
        elif previous and line and _CJK_CHAR.search(previous[-1]) and _CJK_CHAR.search(line[0]):
            # 汉字之间本来就没有空格；折行接上时也不要塞（「世/界」「奥/乐齐」）。
            merged += line
        else:
            merged += " " + line
        previous = line
    return merged




def flow_ocr_quote_body(text: str) -> str:
    """引用旁路里的 OCR：按说话人块收折行，与阅读气泡同一套规则。"""
    parts: list[str] = []
    for block in (text or "").split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        label, sep, rest = block.partition("：")
        if sep and label in {"方伟", "网友"}:
            flowed = _flow_ocr_lines(rest.lstrip("\n"))
            parts.append(f"{label}：{flowed}")
        else:
            parts.append(_flow_ocr_lines(block))
    return "\n\n".join(parts)


def _dialogue_html(text: str) -> str:
    parts: list[str] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        label, sep, rest = block.partition("：")
        if sep and label in {"方伟", "网友"}:
            flowed = _flow_ocr_lines(rest.lstrip("\n"))
            paragraphs = [p for p in flowed.split("\n\n") if p.strip()]
            for index, para in enumerate(paragraphs or [""]):
                body = html_lib.escape(para)
                if index == 0:
                    parts.append(f'<p><span class="who">{label}：</span>{body}</p>')
                else:
                    parts.append(f"<p>{body}</p>")
        else:
            parts.append(f"<p>{html_lib.escape(_flow_ocr_lines(block))}</p>")
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



def _hoist_figures_out_of_paragraphs(html: str) -> str:
    """正文半句里嵌的 <figure> 拎到段外，避免「原图 / 表」插进引语中间。

    ``<p>前<figure/>后</p>`` → ``<p>前</p><figure/><p>后</p>``；空段丢掉。
    figure 内可能还有 ``<p class="ocr-links">``，按嵌套深度找闭合标签。
    """
    if not html or "<figure" not in html.lower():
        return html

    def next_figure(source: str, pos: int) -> tuple[int, int] | None:
        open_match = re.search(r"<figure\b[^>]*>", source[pos:], flags=re.I)
        if open_match is None:
            return None
        start = pos + open_match.start()
        cursor = pos + open_match.end()
        depth = 1
        while depth and cursor < len(source):
            token = re.search(r"</?figure\b[^>]*>", source[cursor:], flags=re.I)
            if token is None:
                return None
            raw = token.group(0)
            cursor = cursor + token.end()
            depth += -1 if raw.lower().startswith("</") else 1
        return None if depth else (start, cursor)

    out: list[str] = []
    pos = 0
    while True:
        span = next_figure(html, pos)
        if span is None:
            out.append(html[pos:])
            break
        start, end = span
        before = html[pos:start]
        figure = html[start:end]
        # Is this figure inside an open <p>? Look at unclosed <p> in `before`.
        opens = list(re.finditer(r"<p\b[^>]*>", before, flags=re.I))
        closes = list(re.finditer(r"</p>", before, flags=re.I))
        inside = bool(opens) and (not closes or opens[-1].start() > closes[-1].start())
        if not inside:
            out.append(before)
            out.append(figure)
            pos = end
            continue
        open_match = opens[-1]
        head = before[: open_match.start()]
        open_tag = open_match.group(0)
        pre = before[open_match.end() :]
        # after figure: consume through the closing </p> of this paragraph
        close = re.search(r"</p>", html[end:], flags=re.I)
        if close is None:
            out.append(before)
            out.append(figure)
            pos = end
            continue
        tail = html[end : end + close.start()]
        pos = end + close.end()
        out.append(head)
        if pre.strip():
            out.append(f"{open_tag}{pre}</p>")
        out.append(figure)
        if tail.strip():
            out.append(f"{open_tag}{tail}</p>")
    cleaned = "".join(out)
    cleaned = re.sub(r"<p\b[^>]*>\s*</p>", "", cleaned, flags=re.I)
    return cleaned


def inline_image_ocr(html: str, texts: dict[str, str]) -> str:
    """有可读识别结果的本地图：群聊先显示整理后的对话，原图是一行「原图 N」链接。

    连续几张微信截图合成一段，重复的句子只出现一次，不再每张图留一个空架子。
    对话里的截图折行收成一段话，不再在半句中间硬换行。商品图和图表只留原图链接。
    表格和长文仍按原文换行，原图同样用「原图 N」。没认出字的图保持原来的图片。
    texts 是 content_hash → image_texts.text。锚点 id 在原图链接上，检索里的「看这张图」仍跳到这里。
    """

    def finish(value: str) -> str:
        # 截图收成一段对话之后，原来夹在图片之间的 <br> 会落到评论标题前面，也会卡在 figure 前后。
        cleaned = strip_breaks_before_comments(value)
        cleaned = re.sub(r"(?:<br\s*/?>\s*)+(?=<figure\b)", "", cleaned, flags=re.I)
        cleaned = re.sub(r"(?<=</figure>)\s*(?:<br\s*/?>\s*)+", "", cleaned, flags=re.I)
        return _hoist_figures_out_of_paragraphs(cleaned)

    if not html or not texts:
        return finish(html or "")
    spans = _image_spans(html)
    if not spans:
        return finish(html)
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
        if item["shot"].kind == "media":
            group = [index]
            nxt = index + 1
            while nxt < len(spans) and parsed[nxt] is not None and parsed[nxt]["shot"].kind == "media":
                group.append(nxt)
                nxt += 1
            outputs[group[0]] = _media_bundle([parsed[cursor] for cursor in group])
            for cursor in group[1:]:
                outputs[cursor] = ""
            index = nxt
            continue
        if item["shot"].kind != "dialogue":
            shown = item["shot"].prose if item["shot"].kind == "prose" else item["raw"]
            outputs[index] = _ocr_figure(item["anchor"], item["src"], item["alt"], shown)
            index += 1
            continue
        group = [index]
        nxt = index + 1
        family = item["shot"].family or "chat"
        while (
            nxt < len(spans)
            and parsed[nxt] is not None
            and parsed[nxt]["shot"].kind == "dialogue"
            and (parsed[nxt]["shot"].family or "chat") == family
        ):
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
    return finish(result)



def _render_markdown(body: str, mapping: dict[str, str]) -> str:
    body = strip_alert_markers(body or "")
    rewritten = rewrite_html_images(rewrite_markdown_images(body, mapping), mapping)
    html = markdown.markdown(
        rewritten,
        extensions=["extra", "nl2br", "sane_lists"],
        output_format="html",
    )
    return _anchor_images(sanitize_html(html))


def render_body(body: str, mapping: dict[str, str], fmt: str | None = None) -> Markup:
    if fmt == "html":
        html = rewrite_html_images(body or "", mapping)
        return Markup(_anchor_images(sanitize_html(html)))
    return Markup(assemble_reader_comments(body or "", mapping, _render_markdown))

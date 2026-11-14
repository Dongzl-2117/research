"""把原文 Markdown 或 HTML 渲染成页面。外部图片不外链。"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Sequence
from html.parser import HTMLParser
from urllib.parse import unquote

import markdown
from markupsafe import Markup

from researchlib.article_layout import mark_unavailable_tables, readable_headings
from researchlib.present_text import polish_display_text, present_article_text
from researchlib.textutil import escape_times, strip_alert_markers
from researchlib.wechat_dialogue import HOST, split_speaker
from researchlib.zsxq_comments import assemble_reader_comments

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


def link_local_images(body: str, mapping: dict[str, str]) -> str:
    """接口给的 markdown：本地存了的图换成 mapping 里的地址，其余的图（还没下载的外链、缺图）照原样留着。"""

    def replace(match: re.Match[str]) -> str:
        target = _replace_destination(match.group(2), mapping)
        return f"![{match.group(1)}]({target})" if target else match.group(0)

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
    """同一张图在一篇里出现几次时只给第一次挂 id：id 重复时 #img-… 只能跳到第一处，页面也不合法（审核 F16）。"""
    seen: set[str] = set()

    def anchor(match: re.Match[str]) -> str:
        name = image_anchor(match.group(3))
        if name in seen:
            return match.group(0)
        seen.add(name)
        return f'<img id="{name}"{match.group(1)}src="{match.group(2)}"'

    return _LOCAL_IMG.sub(anchor, html)


# 识图只用来判断这张图该挨着哪一段，字本身不进阅读页。
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_P_BLOCK = re.compile(r"<p\b[^>]*>.*?</p>", re.I | re.S)
_HASH_IN_SRC = re.compile(r"""\bsrc\s*=\s*["'][^"']*?([0-9a-f]{64})""", re.I)
_BR_AFTER = re.compile(r"\s*<br\s*/?>", re.I)
_EMPTY_P = re.compile(r"<p\b[^>]*>(?:\s|<br\s*/?>)*</p>", re.I)
_YEAR_VALUE = re.compile(r"((?:19|20)\d{2})\D{0,8}(\d{2,6}(?:\.\d+)?)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_PERCENT = re.compile(r"%")
# 短过这个的重合不当成「这张图就是在讲这一段」，免得年份、常用词把图拖走。
_PHRASE = 5


def _plain_fragment(fragment: str) -> str:
    text = _IMG_TAG.sub(" ", fragment)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text)


def _cjk(text: str) -> str:
    return "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")


def _phrase_index(texts: list[str]) -> dict[str, list[tuple[int, int]]]:
    """每个 ``_PHRASE`` 字的片段出现在第几段的第几个字。一页的段落建一次，每张图都拿来查。"""
    index: dict[str, list[tuple[int, int]]] = {}
    for number, text in enumerate(texts):
        for start in range(len(text) - _PHRASE + 1):
            index.setdefault(text[start : start + _PHRASE], []).append((number, start))
    return index


def _phrase_scores(ocr: str, texts: list[str], index: dict[str, list[tuple[int, int]]]) -> list[int]:
    """识图的汉字和每一段汉字最长的一段重合，最多算到 24 字，短于 ``_PHRASE`` 记 0。

    index 是 ``_phrase_index(texts)``。长过 ``_PHRASE`` 的重合一定从两边都有的某个片段开始，所以只从这些片段
    往后接着比。以前对每一段都从 24 字到 5 字逐个长度把识图文字切一遍再查，阅读页每次打开都现算：
    30 张图、100 段的公众号文章要算 3 秒左右，60 张图要 12 秒。
    """
    scores = [0] * len(texts)
    if len(ocr) < _PHRASE:
        return scores
    caps = [min(24, len(ocr), len(text)) for text in texts]
    for at in range(len(ocr) - _PHRASE + 1):
        for number, start in index.get(ocr[at : at + _PHRASE], ()):
            text = texts[number]
            cap = caps[number]
            if scores[number] >= cap or (at and start and ocr[at - 1] == text[start - 1]):
                # 已经到顶；或者往前还接得上，这里是前一处那段重合的后半截，前一处已经比过，只会更长。
                continue
            size = _PHRASE
            while (
                size < cap
                and at + size < len(ocr)
                and start + size < len(text)
                and ocr[at + size] == text[start + size]
            ):
                size += 1
            if size > scores[number]:
                scores[number] = size
    return scores


def _phrase_overlap(left: str, right: str) -> int:
    """两边汉字里最长的一段重合，最多算到 24 字。短于 ``_PHRASE`` 记 0。"""
    return _phrase_scores(left, [right], _phrase_index([right]))[0]


def _table_score(ocr: str, paragraph: str) -> int:
    """按年排的数据表：段落里同时出现表上的年份和某个数值（百分号后面的不算）才算对上。"""
    rows = _YEAR_VALUE.findall(ocr)
    if len(rows) < 5:
        return 0
    years = {year for year, _value in rows}
    values: set[str] = set()
    for _year, raw in rows:
        number = int(float(raw))
        if number >= 10:
            values.add(str(number))
    seen_years: set[str] = set()
    seen_values: set[str] = set()
    for match in _NUMBER.finditer(paragraph):
        raw = match.group(0)
        if _PERCENT.match(paragraph, match.end()):
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", raw.split(".", 1)[0]):
            seen_years.add(raw.split(".", 1)[0])
            continue
        number = int(float(raw))
        if number >= 10:
            seen_values.add(str(number))
    if not (years & seen_years) or not (values & seen_values):
        return 0
    return len(values & seen_values) * 10 + len(years & seen_years)


def _best_paragraph(
    ocr: str,
    paragraphs: list[str],
    cjk_paragraphs: list[str] | None = None,
    index: dict[str, list[tuple[int, int]]] | None = None,
) -> int | None:
    if cjk_paragraphs is None:
        cjk_paragraphs = [_cjk(paragraph) for paragraph in paragraphs]
    if index is None:
        index = _phrase_index(cjk_paragraphs)
    phrases = _phrase_scores(_cjk(ocr), cjk_paragraphs, index)
    if phrases and max(phrases) >= _PHRASE:
        best = max(phrases)
        winners = [index for index, score in enumerate(phrases) if score == best]
        return winners[0] if len(winners) == 1 else None
    tables = [_table_score(ocr, paragraph) for paragraph in paragraphs]
    if not tables or max(tables) <= 0:
        return None
    best = max(tables)
    winners = [index for index, score in enumerate(tables) if score == best]
    return winners[0] if len(winners) == 1 else None


def _relocate_images(html: str, texts: dict[str, str]) -> str:
    """图被估到了别的段里时，按识图里的字挪回对得上的那一段前面。

    挪的是 ``<img>`` 本身。识图文字不写进正文。已经挨着对的段、对不上、或两段一样像，就留在原地。
    评论区里的图不动。没有改动时返回原来的字符串。
    """
    comments_at = html.find('class="zsxq-comments"')
    paragraphs = []
    for match in _P_BLOCK.finditer(html):
        if comments_at != -1 and match.start() >= comments_at:
            continue
        paragraphs.append((match.start(), match.end(), _plain_fragment(match.group(0))))
    if not paragraphs:
        return html
    plain = [item[2] for item in paragraphs]
    cjk_plain = [_cjk(text) for text in plain]
    index = _phrase_index(cjk_plain)
    moves: list[tuple[int, int, str, int]] = []
    for image in _IMG_TAG.finditer(html):
        if comments_at != -1 and image.start() >= comments_at:
            continue
        found = _HASH_IN_SRC.search(image.group(0))
        if found is None:
            continue
        ocr = (texts.get(found.group(1)) or "").strip()
        if len(_cjk(ocr)) < _PHRASE and len(_YEAR_VALUE.findall(ocr)) < 5:
            continue
        target = _best_paragraph(ocr, plain, cjk_plain, index)
        if target is None:
            continue
        containing = next(
            (index for index, item in enumerate(paragraphs) if item[0] <= image.start() < item[1]),
            None,
        )
        if containing == target:
            continue
        if containing is None:
            previous = next(
                (
                    index
                    for index in range(len(paragraphs) - 1, -1, -1)
                    if paragraphs[index][1] <= image.start()
                ),
                None,
            )
            following = next(
                (index for index, item in enumerate(paragraphs) if item[0] > image.start()),
                None,
            )
            if target in {previous, following}:
                continue
        end = image.end()
        broken = _BR_AFTER.match(html, end)
        if broken:
            end = broken.end()
        start = image.start()
        if containing is not None:
            inner_from = html.find(">", paragraphs[containing][0]) + 1
            inner_to = paragraphs[containing][1] - len("</p>")
            leftover = html[inner_from:start] + html[end:inner_to]
            # 同段里还有别的图时不能把整段删掉，否则两张图的删除区间叠在一起。
            if not _IMG_TAG.search(leftover) and not _plain_fragment(leftover).strip():
                start, end = paragraphs[containing][0], paragraphs[containing][1]
        moves.append((start, end, image.group(0), paragraphs[target][0]))
    if not moves:
        return html
    cuts: list[tuple[int, int]] = []
    for start, end, _img, _target in moves:
        if (start, end) not in cuts:
            cuts.append((start, end))

    def adjust(pos: int) -> int:
        return pos - sum(end - start for start, end in cuts if end <= pos)

    grouped: dict[int, list[str]] = {}
    for _start, _end, img, target in sorted(moves, key=lambda item: item[0]):
        grouped.setdefault(adjust(target), []).append(img)
    updated = html
    for start, end, _img, _target in sorted(moves, key=lambda item: item[0], reverse=True):
        updated = updated[:start] + updated[end:]
    for pos in sorted(grouped, reverse=True):
        updated = updated[:pos] + "".join(grouped[pos]) + updated[pos:]
    return _EMPTY_P.sub("", updated)


# 极致了的公众号正文按段估图的位置（第 N 张图放在第 N 段后面），图会整组堆在开头的引文里（#244）。
# 别的来源图就在原文的位置：飞书、星球、Stratechery、雪球存档；孤独进化体的正文先从公众号原文页取。
# 按识图挪只会挪错：飞书一张利润表截图跟在「下图是年报里的合并利润表：」后面，和下一节的一段同有
# 「归属于母公司股东的净利润」，比「合并利润表」长，就被挪到了那一段前面。
_ESTIMATED_IMAGE_KINDS = frozenset({"wechat_mp"})


def images_placed_by_estimate(kind: str, meta: dict | None = None) -> bool:
    """这篇的图位置是估的，阅读页可以按识图把图挪回对得上的段落（``inline_image_ocr``）。

    只有方伟公众号（kind ``wechat_mp``）以前的正文是极致了按段估的位置。正文从公众号原文页取的
    （归档头「- 图片位置：原文页」，导入时记成 ``raw_meta.images = "page"``）图本来就在原位，不挪。
    """
    if kind not in _ESTIMATED_IMAGE_KINDS:
        return False
    return (meta or {}).get("images") != "page"


def inline_image_ocr(html: str, texts: dict[str, str] | None = None, *, host: str = HOST) -> str:
    """阅读页保留原图，不把识图文字铺进版面。

    ``texts`` 是 content_hash → image_texts.text，``host`` 是这篇作者。字只进摘要、观点、问这篇和检索
    （``compose_ocr_text`` / ``append_ocr`` / ``ocr_chunks``）。点开图片仍走原来的 ``<img>``
    （锚点 ``img-`` 还在图上，检索「看这张图」跳到这张图）。

    极致了按段估的图片会整组堆在开头、插进无关的引文里。这里只用识图里的字判断该挨着哪一段，
    把 ``<img>`` 挪到那一段前面；对不上、或本来就挨着，原样留着。正文不出现识图文字。
    图位置本来就对的来源不要传 ``texts``（见 ``images_placed_by_estimate``）。
    """
    _ = host
    if not html or not texts:
        return html or ""
    return _relocate_images(html, texts)


def lazy_images(html: str) -> str:
    """阅读页正文里的图滚到附近才下（``loading="lazy"``），解码不挡页面（``decoding="async"``）。

    /media 回 ``private, no-store``，浏览器不留图：以前每打开一次文章（切「对照」「原文」也算），
    几十张图一起从 Mac mini 经隧道重下一遍，第一屏的图要跟后面几十张抢带宽，没往下读的也照样下完。
    只在交给模板前最后加：译制、对照核验和回执用的原文 HTML 不带这两个属性
    （``canonical_body_html`` 一改，旧译文的回执就对不上）。
    """

    def add(match: re.Match[str]) -> str:
        tag = match.group(0)
        if re.search(r"\sloading\s*=", tag, re.I):
            return tag
        closing = " />" if tag.endswith("/>") else ">"
        head = tag[: -len(closing.strip())].rstrip()
        return f'{head} loading="lazy" decoding="async"{closing}'

    return _IMG_TAG.sub(add, html or "")


_BODY_WHO = re.compile(
    r"(<p\b[^>]*>)(\s*)([^\s：:，。！？!?<>]{1,16})([：:])",
)


def mark_dialogue_speakers(html: str) -> str:
    """正文和评论里行首的「方伟：」「网友：」「昵称：」加粗，和 OCR 对话同一套 .who。"""

    def replace(match: re.Match[str]) -> str:
        open_tag, space, who, colon = match.groups()
        if split_speaker(f"{who}{colon}", names=True) is None:
            return match.group(0)
        return f'{open_tag}{space}<span class="who">{who}{colon}</span>'

    return _BODY_WHO.sub(replace, html or "")


# 星球的话题标签单独占一行（「#拼多多#」「#拼多多# #Temu#」）。Python-Markdown 不要求 # 后面有空格，
# 会把它渲染成一级标题。
_HASHTAG_LINE = re.compile(r"^( {0,3})#(?=[^#\s][^#\n]*#(?:[ \t]*#[^#\s][^#\n]*#)*[ \t]*$)")


def _escape_hashtag_lines(body: str) -> str:
    """话题标签行行首的 # 转义掉，照原样显示成一行字。代码块里不动。"""
    if "#" not in body:
        return body
    lines = body.split("\n")
    fenced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced:
            lines[index] = _HASHTAG_LINE.sub(r"\1\\#", line, count=1)
    return "\n".join(lines)


def _render_markdown(body: str, mapping: dict[str, str]) -> str:
    body = escape_times(_escape_hashtag_lines(present_article_text(strip_alert_markers(body or ""))))
    rewritten = rewrite_html_images(rewrite_markdown_images(body, mapping), mapping)
    html = markdown.markdown(
        rewritten,
        extensions=["extra", "nl2br", "sane_lists"],
        output_format="html",
    )
    return mark_unavailable_tables(readable_headings(mark_dialogue_speakers(_anchor_images(sanitize_html(html)))))


def render_body(body: str, mapping: dict[str, str], fmt: str | None = None, *, owners: Sequence[str] = ()) -> Markup:
    """owners：评论区只留这些名字（来源作者，见 zsxq_comments.source_owners）。不给时按方伟认。"""
    if fmt == "html":
        html = rewrite_html_images(polish_display_text(body or ""), mapping)
        return Markup(mark_unavailable_tables(readable_headings(_anchor_images(sanitize_html(html)))))
    return Markup(assemble_reader_comments(body or "", mapping, _render_markdown, owners))


def canonical_body_html(
    body_md: str, title: str, mapping: dict[str, str], fmt: str | None, *, owners: Sequence[str] = (),
) -> str:
    """翻译和页面共用的原始渲染，不加临时段落号、站内链接改写或图片文字。"""
    from researchlib.textutil import split_frontmatter, strip_duplicate_title

    _frontmatter, body = split_frontmatter(body_md or "")
    if fmt != "html":
        body = strip_duplicate_title(body, title)
    return str(render_body(body, mapping, fmt, owners=owners))

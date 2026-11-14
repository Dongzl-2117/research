"""在原文里高亮检索词。不用 ts_headline，避免二字分词把片段打乱。"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from markupsafe import Markup, escape

from researchlib.htmlentities import entity_ref
from researchlib.output_safety import redact_subscription_links
from researchlib.textutil import escape_times

_TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*|[\u4e00-\u9fff]+")


def extract_terms(query: str) -> list[str]:
    terms: list[str] = []
    for match in _TERM.finditer(query or ""):
        term = match.group(0)
        if term.isascii() and len(term) < 2:
            continue
        if term not in terms:
            terms.append(term)
    return terms


def _find(text: str, term: str) -> int:
    if term.isascii():
        return text.casefold().find(term.casefold())
    return text.find(term)


def _find_all(text: str, term: str) -> list[int]:
    haystack, needle = (text.casefold(), term.casefold()) if term.isascii() else (text, term)
    found: list[int] = []
    index = haystack.find(needle)
    while index >= 0:
        found.append(index)
        index = haystack.find(needle, index + max(len(needle), 1))
    return found


def _best_hit(flat: str, groups: list[tuple[float, list[str]]], window: int) -> int | None:
    """窗口放在哪个命中处：窗口里写到的词组按权重（少见程度）加起来最多的；一样多时取靠前的。

    以前放在第一处命中：「Costco 商业模式」的摘录常常停在开头名人语录里的「商业」两个字上，
    后面整段在讲 Costco 的会员和低毛利，卡片上却看不到。
    一样多时不挑命中次数多的：那样常挑中「护城河、护城河、护城河」这种只重复词的地方，丢掉前面下定义的那句。
    """
    hits: list[tuple[int, int]] = []  # (位置, 第几个词组)
    for number, (_weight, forms) in enumerate(groups):
        for form in forms:
            if form:
                hits.extend((index, number) for index in _find_all(flat, form))
    if not hits:
        return None
    hits.sort()
    best: tuple[float, int] | None = None
    for center, _number in hits:
        start = max(0, center - window // 2)
        inside = {number for index, number in hits if start <= index < start + window}
        key = (sum(groups[number][0] for number in inside), -center)
        if best is None or key > best:
            best = key
    return -best[1] if best else None


def highlight_snippet(
    text: str, query: str, *, radius: int = 88, terms: list[str] | None = None,
    groups: list[tuple[float, list[str]]] | None = None,
) -> Markup:
    """取命中附近的一段并高亮。terms 给了就用它（检索实际用到的词，含公司别名和同义词）。

    groups 是检索的词组和各自的权重（[(权重, [这组的各种写法])]）：给了就把窗口放在词组最集中、最少见的地方，
    没给就放在第一处命中。
    """
    # 先遮地址再截窗口；截掉协议或把凭据拆进 <mark> 后，输出边界就认不全了。
    flat = re.sub(r"\s+", " ", redact_subscription_links(text or "")).strip()
    if not flat:
        return Markup("")
    terms = [t for t in (terms if terms is not None else extract_terms(query)) if t]
    positions = [index for term in terms if (index := _find(flat, term)) >= 0]
    if not positions:
        fragment = flat[: radius * 2]
        suffix = "…" if len(flat) > len(fragment) else ""
        return Markup(str(escape(fragment)) + suffix)
    window = max(radius, 48)
    start_hit = _best_hit(flat, groups, window) if groups else None
    if start_hit is None:
        start_hit = min(positions)
    start = max(0, start_hit - window // 2)
    end = min(len(flat), start + window)
    if end - start < window:
        start = max(0, end - window)
    fragment = flat[start:end]
    prefix = "…" if start else ""
    suffix = "…" if end < len(flat) else ""
    return Markup(prefix + _mark(fragment, terms) + suffix)


def highlight_title(title: str, query: str, *, terms: list[str] | None = None) -> Markup:
    return Markup(_mark(title or "", [t for t in (terms if terms is not None else extract_terms(query)) if t]))


def highlight_full(text: str, terms: list[str]) -> Markup:
    """整段原文，命中处加 <mark>。问库页展开原文时用。"""
    return Markup(_mark(text or "", [t for t in terms if t]))


def _mark(text: str, terms: list[str]) -> str:
    text = redact_subscription_links(text or "")
    if not text or not terms:
        return str(escape(text))
    escaped = str(escape(text))
    safe_terms = sorted({term for term in terms if term}, key=len, reverse=True)
    alternatives = "|".join(re.escape(term) for term in safe_terms)
    if not alternatives:
        return escaped
    # 连在一起的几个词（「广告」「加载」）合成一处高亮，不拆成两块。
    pattern = f"(?:{alternatives})+"
    flags = re.IGNORECASE if any(term.isascii() for term in safe_terms) else 0
    return re.sub(pattern, lambda match: f"<mark>{match.group(0)}</mark>", escaped, flags=flags)


# 提及原句的边界：中文句号、问号、叹号、分号和换行；英文的句点后面要跟着空白（「2.5 亿」「Amazon.com」不算）。
_SENTENCE_EDGE = re.compile(r"[。！？!?；;\n]|\.(?=\s)")
# 句末后面跟着的右引号、右括号还属于上一句。
_CLOSERS = "”’」』）)】》\"'"
# 提及所在的一行没有句末标点、又这么短（「-拼多多」「腾讯：」这种话题行）：接着取到下一句。
_SHORT_LINE = 16


def _word_cut(text: str, index: int) -> bool:
    """index 前后都是英文字母或数字：在这里截会截在半个词上。"""
    if index <= 0 or index >= len(text):
        return False
    left, right = text[index - 1], text[index]
    return left.isascii() and left.isalnum() and right.isascii() and right.isalnum()


def mention_window(text: str, start_hit: int, end_hit: int, radius: int) -> tuple[int, int, bool, bool]:
    """提及原句：从命中处往前找最近的句末、往后找最近的句末，各不超过 radius 字。

    句子更长时在 radius 处截，英文不截在半个词上。返回 (起, 止, 前面截过, 后面截过)。
    """
    start = max(0, start_hit - radius)
    edges = list(_SENTENCE_EDGE.finditer(text, start, start_hit))
    cut_start = False
    if edges:
        start = edges[-1].end()
        while start < start_hit and (text[start] in _CLOSERS or text[start].isspace()):
            start += 1
    elif start:
        cut_start = True
        if _word_cut(text, start):
            space = text.find(" ", start, start_hit)
            if space >= 0:
                start = space + 1
    end = min(len(text), end_hit + radius)
    cut_end = end < len(text)
    for edge in _SENTENCE_EDGE.finditer(text, end_hit, end):
        if edge.group(0) == "\n":
            if edge.start() - start < _SHORT_LINE:
                continue
            end, cut_end = edge.start(), False
            break
        stop = edge.end()
        while stop < len(text) and text[stop] in _CLOSERS:
            stop += 1
        end, cut_end = stop, False
        break
    if cut_end and _word_cut(text, end):
        space = text.rfind(" ", end_hit, end)
        if space >= 0:
            end = space
    return start, end, cut_start, cut_end


def mention_spans(matcher, title: str, body_text: str, ocr_text: str = "") -> tuple[str, list[tuple[int, int, int]]]:
    """公司提及在哪段文字里、在哪：先看正文，正文里没有再看图片里的字（截图帖的提及常常只在图里，
    短正文的提及本来就连图片里的字一起算）。matcher 是只放了这家公司的 CompanyMatcher。"""
    body = body_text or ""
    spans = matcher.document(title, body).spans(body)
    if spans or not (ocr_text or "").strip():
        return body, spans
    return ocr_text, matcher.document(title, body, ocr_text).spans(ocr_text)


def mention_snippet(text: str, spans: list[tuple[int, int, int]], *, radius: int = 70) -> Markup:
    """公司页用：第一处命中所在的那句（前后各不超过 radius 字），命中处加 <mark>。spans 来自 CompanyMatcher.spans。"""
    if not text or not spans:
        return Markup("")
    start_hit, end_hit, _company = spans[0]
    start, end, cut_start, cut_end = mention_window(text, start_hit, end_hit, radius)
    before = re.sub(r"\s+", " ", text[start:start_hit])
    hit = text[start_hit:end_hit]
    after = re.sub(r"\s+", " ", text[end_hit:end]).rstrip()
    prefix = "…" if cut_start else ""
    suffix = "…" if cut_end else ""
    return Markup(f"{prefix}{escape(before)}<mark>{escape(hit)}</mark>{escape(after)}{suffix}")


def mention_sentence(text: str, spans: list[tuple[int, int, int]], *, radius: int = 80) -> str:
    """接口用：第一处命中所在的那句，纯文本，和公司页同一个取法。"""
    if not text or not spans:
        return ""
    start_hit, end_hit, _company = spans[0]
    start, end, cut_start, cut_end = mention_window(text, start_hit, end_hit, radius)
    sentence = " ".join(text[start:end].split())
    return ("…" if cut_start else "") + sentence + ("…" if cut_end else "")


_CITE = re.compile(r"\[(\d+)\]")


class _AnswerCitations(HTMLParser):
    """只改正文文字；代码、链接及标签属性里的 [n] 保持原样。

    方括号包在 cite-mark 里：页面上只显示小圆角数字，复制出来、读屏读到的仍是「[n]」。
    """

    def __init__(self, citations: list[dict] | None) -> None:
        super().__init__(convert_charrefs=False)
        self.known = None if citations is None else {str(item.get("n")) for item in citations}
        self.out: list[str] = []
        self.excluded = 0

    def handle_starttag(self, tag, attrs):
        self.out.append(self.get_starttag_text())
        if tag in {"a", "code", "pre"}:
            self.excluded += 1

    def handle_endtag(self, tag):
        self.out.append(f"</{tag}>")
        if tag in {"a", "code", "pre"}:
            self.excluded = max(0, self.excluded - 1)

    def handle_data(self, data):
        if not self.excluded:
            data = _CITE.sub(
                lambda match: (
                    f'<a class="cite" href="#c-{match[1]}">'
                    f'<span class="cite-mark">[</span>{match[1]}<span class="cite-mark">]</span></a>'
                    if self.known is None or match[1] in self.known else match[0]
                ), data,
            )
        self.out.append(data)

    def handle_entityref(self, name):
        self.out.append(entity_ref(name))

    def handle_charref(self, name):
        self.out.append(f"&#{name};")


def answer_html(text: str, citations: list[dict] | None = None) -> Markup:
    """问库共用 Markdown 排版；禁用原始 HTML，再清理链接，引用仅指向已知原文。"""
    import markdown

    from researchlib.render import sanitize_html

    renderer = markdown.Markdown(extensions=["sane_lists", "fenced_code", "tables"])
    renderer.preprocessors.deregister("html_block")
    renderer.inlinePatterns.deregister("html")
    html = sanitize_html(renderer.convert(escape_times(text or "")))
    parser = _AnswerCitations(citations)
    parser.feed(html)
    parser.close()
    return Markup("".join(parser.out))

"""在原文里高亮检索词。不用 ts_headline，避免二字分词把片段打乱。"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

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


def highlight_snippet(text: str, query: str, *, radius: int = 88, terms: list[str] | None = None) -> Markup:
    """取第一处命中附近的一段并高亮。terms 给了就用它（检索实际用到的词，含公司别名和同义词）。"""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return Markup("")
    terms = [t for t in (terms if terms is not None else extract_terms(query)) if t]
    positions = [index for term in terms if (index := _find(flat, term)) >= 0]
    if not positions:
        fragment = flat[: radius * 2]
        suffix = "…" if len(flat) > len(fragment) else ""
        return Markup(str(escape(fragment)) + suffix)
    start_hit = min(positions)
    window = max(radius, 48)
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


def mention_snippet(text: str, spans: list[tuple[int, int, int]], *, radius: int = 70) -> Markup:
    """公司页用：取第一处命中前后各 radius 字，命中处加 <mark>。spans 来自 CompanyMatcher.spans。"""
    if not text or not spans:
        return Markup("")
    start_hit, end_hit, _company = spans[0]
    start = max(0, start_hit - radius)
    end = min(len(text), end_hit + radius)
    before = re.sub(r"\s+", " ", text[start:start_hit])
    hit = text[start_hit:end_hit]
    after = re.sub(r"\s+", " ", text[end_hit:end])
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return Markup(f"{prefix}{escape(before)}<mark>{escape(hit)}</mark>{escape(after)}{suffix}")


_CITE = re.compile(r"\[(\d{1,3})\]")


def answer_html(text: str) -> Markup:
    """LLM 回答转成 HTML。先转义再走 Markdown，回答里的 HTML 不会生效；[n] 换成跳到出处的链接。"""
    import markdown

    html = markdown.markdown(str(escape(text or "")), extensions=["sane_lists"])
    return Markup(_CITE.sub(lambda m: f'<a class="cite" href="#c-{m.group(1)}">[{m.group(1)}]</a>', html))

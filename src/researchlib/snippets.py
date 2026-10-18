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


def highlight_snippet(text: str, query: str, *, radius: int = 88) -> Markup:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return Markup("")
    terms = extract_terms(query)
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


def highlight_title(title: str, query: str) -> Markup:
    return Markup(_mark(title or "", extract_terms(query)))


def _mark(text: str, terms: list[str]) -> str:
    if not text or not terms:
        return str(escape(text))
    escaped = str(escape(text))
    safe_terms = sorted({term for term in terms if term}, key=len, reverse=True)
    pattern = "|".join(re.escape(term) for term in safe_terms)
    if not pattern:
        return escaped
    flags = re.IGNORECASE if any(term.isascii() for term in safe_terms) else 0
    return re.sub(pattern, lambda match: f"<mark>{match.group(0)}</mark>", escaped, flags=flags)

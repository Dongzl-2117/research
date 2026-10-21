"""把「读者问 / 段永平」渲染成问答块，不再是两级标题加一整段。

读者的问题用引用样式，长文按句子拆开。段永平那节里的「回本文开头」不显示。
只有导航、没有他的话时，回答处写「没有有效回答」。
"""

from __future__ import annotations

import re

import markdown
from markupsafe import escape

from researchlib.empty_answer import substantive_voice, voice_is_empty
from researchlib.render import render_body, rewrite_markdown_images, sanitize_html

_H2 = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")
_ASK = frozenset({"读者问"})
_ANSWER = frozenset({"段永平", "作者", "作者答"})
_LIMIT = 200


def split_h2(md: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(_H2.finditer(md or ""))
    if not matches:
        return (md or "").strip(), []
    preamble = md[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(md)
        sections.append((match.group(1).strip(), md[match.end() : end].strip()))
    return preamble, sections


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        buf.append(char)
        ended = char in "。！？!?；;"
        if not ended and char == "." and index + 1 < size:
            prev = text[index - 1] if index else ""
            rest = text[index + 1 :].lstrip()
            abbrev = bool(prev.isupper() and (index < 2 or not text[index - 2].isalpha()))
            ended = not abbrev and bool(rest[:1].isupper())
        if ended:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            while index + 1 < size and text[index + 1] == " ":
                index += 1
        index += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def split_paragraphs(text: str, limit: int = _LIMIT) -> list[str]:
    """空行是段落。一段长得读不完时，按句号拆开再拼回大约两百字一段。"""
    cleaned = substantive_voice(text)
    if not cleaned:
        return []
    blocks = [re.sub(r"[ \t]*\n[ \t]*", "", block).strip() for block in re.split(r"\n\s*\n", cleaned)]
    paragraphs: list[str] = []
    for block in blocks:
        if not block:
            continue
        if len(block) <= limit:
            paragraphs.append(block)
            continue
        sentences = _sentences(block)
        if len(sentences) <= 1:
            paragraphs.append(block)
            continue
        buf = ""
        for sentence in sentences:
            nxt = sentence
            if buf and buf[-1] in ".!?":
                nxt = " " + nxt
            if buf and len(buf) + len(nxt) > limit:
                paragraphs.append(buf)
                buf = sentence
            else:
                buf += nxt
        if buf:
            paragraphs.append(buf)
    return paragraphs


def _kind(title: str) -> str:
    if title in _ASK:
        return "ask"
    if title in _ANSWER:
        return "answer"
    return "aside"


def _is_qa(sections: list[tuple[str, str]]) -> bool:
    titles = {title for title, _body in sections}
    return bool(titles & _ASK) and bool(titles & _ANSWER)


def _inner_html(text: str) -> str:
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return ""
    rendered = markdown.markdown(
        "\n\n".join(paragraphs),
        extensions=["extra", "sane_lists"],
        output_format="html",
    )
    return sanitize_html(rendered)


def _block(title: str, body: str) -> str:
    kind = _kind(title)
    if kind == "answer" and voice_is_empty(body):
        inner = '<p class="qa-missing">没有有效回答。</p>'
    else:
        inner = _inner_html(body)
        if not inner:
            return ""
    label = str(escape(title))
    return (
        f'<section class="qa-block qa-{kind}">'
        f'<p class="qa-label">{label}</p>'
        f'<div class="qa-body">{inner}</div>'
        f"</section>"
    )


def render_qa_html(body: str, mapping: dict[str, str] | None = None) -> str | None:
    """是问答结构就返回 HTML，否则返回 None，调用方照旧渲染。"""
    rewritten = rewrite_markdown_images(body or "", mapping or {})
    preamble, sections = split_h2(rewritten)
    if not _is_qa(sections):
        return None
    parts: list[str] = []
    if preamble:
        parts.append(str(render_body(preamble, {})))
    for title, section in sections:
        block = _block(title, section)
        if block:
            parts.append(block)
    return "".join(parts)

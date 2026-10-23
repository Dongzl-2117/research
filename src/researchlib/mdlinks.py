"""飞书导出的相对 .md 链接。

正文里大量 `[标题](标题__token.md)`。渲染后浏览器把它接到 `/articles/<当前 id>/` 后面，
路由只接受数字 id，于是 422。这里在 `render_body` 之后、`stamp_paragraph_ids` 之前，
按同一来源的 external_id 改成 `/articles/<id>`。对不上的去掉链接，留下文字和「未收录」。

external_id 是归档里的相对路径（飞书是 `docs/<文件名>.md`）。文件名对不上时，再用
`__token` 后缀做唯一匹配。http(s)、站内绝对路径和 `#锚点` 不动。
"""

from __future__ import annotations

import html as html_lib
import re

from markupsafe import escape

from researchlib.attachments import rewrite_relative_attachment_links
from researchlib.qa_layout import render_qa_html
from researchlib.render import render_body
from researchlib.textutil import split_frontmatter, strip_duplicate_title
from researchlib.translate.pair import stamp_paragraph_ids

_FENCE = re.compile(r"```.*?```", re.S)
_MD_LINK = re.compile(r"(?<!!)\[(?:[^\[\]]*)\]\(\s*<?([^)\s>]+)>?")
_REF_DEF = re.compile(r"(?m)^[ \t]{0,3}\[[^\]]+\]:[ \t]*<?([^\s>]+)>?")
_HTML_HREF = re.compile(r"""(?i)\bhref\s*=\s*["']([^"']+)["']""")
_A_TAG = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF_ATTR = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_TOKEN = re.compile(r"__([A-Za-z0-9]{4,})$")
_TAG = re.compile(r"<[^>]+>")


def filename_token(name: str) -> str | None:
    base = (name or "").rsplit("/", 1)[-1]
    base = re.sub(r"\.(?:md|markdown)$", "", base, flags=re.I)
    match = _TOKEN.search(base)
    return match.group(1) if match else None


def relative_md_path(href: str) -> str | None:
    """相对 .md 路径。http(s)、`//`、`/`、`#` 开头的返回 None。"""
    raw = html_lib.unescape((href or "").strip())
    if not raw or raw.startswith(("#", "/", "?")):
        return None
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "javascript:", "//", "data:")):
        return None
    path = raw.split("#", 1)[0].split("?", 1)[0].strip().strip("<>")
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path.lower().endswith((".md", ".markdown")):
        return None
    return path


def relative_md_hrefs(html: str) -> list[str]:
    """渲染结果里仍指向相对 .md 的 href。回归测试用。"""
    found = []
    for _quote, href in _HREF_ATTR.findall(html or ""):
        if relative_md_path(href) is not None:
            found.append(href)
    return found


def iter_link_targets(text: str) -> list[str]:
    """Markdown / HTML 里的链接目标，按出现顺序。代码块里的不算。"""
    cleaned = _FENCE.sub(" ", text or "")
    found: list[tuple[int, str]] = []
    for pattern in (_MD_LINK, _REF_DEF, _HTML_HREF):
        for match in pattern.finditer(cleaned):
            href = match.group(1).strip()
            if href:
                found.append((match.start(), href))
    found.sort(key=lambda item: item[0])
    return [href for _pos, href in found]


def _unique(ids: list[int]) -> int | None:
    found = list(dict.fromkeys(ids))
    if len(found) == 1:
        return found[0]
    return None


class MdIndex:
    """同一来源的 external_id → 文章 id。"""

    def __init__(self, rows) -> None:
        self.by_external: dict[str, int] = {}
        self.by_name: dict[str, list[int]] = {}
        self.by_token: dict[str, list[int]] = {}
        self.by_token_fold: dict[str, list[int]] = {}
        for row in rows:
            external_id = str(row["external_id"])
            article_id = int(row["id"])
            self.by_external.setdefault(external_id, article_id)
            name = external_id.rsplit("/", 1)[-1]
            self.by_name.setdefault(name.casefold(), []).append(article_id)
            token = filename_token(name)
            if token:
                self.by_token.setdefault(token, []).append(article_id)
                self.by_token_fold.setdefault(token.casefold(), []).append(article_id)

    def resolve(self, href: str, parent: str = "") -> int | None:
        target = relative_md_path(href)
        if not target:
            return None
        name = target.rsplit("/", 1)[-1]
        parent = (parent or "").strip("/")
        candidates: list[str] = []

        def add(value: str) -> None:
            value = value.strip("/")
            if value and value not in candidates:
                candidates.append(value)

        add(target)
        add(name)
        if parent:
            add(f"{parent}/{name}")
            if not target.startswith(parent + "/"):
                add(f"{parent}/{target}")
        for key in candidates:
            found = self.by_external.get(key)
            if found is not None:
                return found
        by_name = _unique(self.by_name.get(name.casefold(), []))
        if by_name is not None:
            return by_name
        token = filename_token(name)
        if not token:
            return None
        exact = _unique(self.by_token.get(token, []))
        if exact is not None:
            return exact
        return _unique(self.by_token_fold.get(token.casefold(), []))


def load_md_index(conn, source_id: str) -> MdIndex:
    rows = conn.execute(
        """
        SELECT id, external_id
        FROM articles
        WHERE source_id = %s AND duplicate_of IS NULL
        """,
        (source_id,),
    ).fetchall()
    return MdIndex(rows)


def rewrite_relative_md_links(html: str, index: MdIndex, parent: str = "") -> str:
    """把相对 .md 的 `<a href>` 改成文章页。对不上就留下文字，标「未收录」。"""

    def replace(match: re.Match[str]) -> str:
        attrs, inner = match.group(1), match.group(2)
        href_match = _HREF_ATTR.search(attrs)
        if href_match is None:
            return match.group(0)
        href = href_match.group(2)
        if relative_md_path(href) is None:
            return match.group(0)
        article_id = index.resolve(href, parent)
        if article_id is None:
            label = _TAG.sub("", inner).strip()
            if not label:
                name = (relative_md_path(href) or "").rsplit("/", 1)[-1]
                label = str(escape(name))
                return f'{label}<span class="doc-missing">未收录</span>'
            return f'{inner}<span class="doc-missing">未收录</span>'
        new_attrs = _HREF_ATTR.sub(f'href="/articles/{article_id}"', attrs, count=1)
        return f"<a{new_attrs}>{inner}</a>"

    return _A_TAG.sub(replace, html)


def finish_html(
    html: str,
    index: MdIndex | None,
    parent: str = "",
    attachments: dict[str, str] | None = None,
) -> str:
    """渲染之后、段落编号之前：先改相对 .md 链接，再盖 p-id。"""
    if html and ".md" in html.lower():
        html = rewrite_relative_md_links(html, index or MdIndex([]), parent)
    return stamp_paragraph_ids(rewrite_relative_attachment_links(html, attachments))


def prepare_body_html(
    body_md: str,
    title: str,
    mapping: dict[str, str],
    fmt: str | None,
    index: MdIndex | None,
    parent: str = "",
    attachments: dict[str, str] | None = None,
) -> str:
    _frontmatter, body = split_frontmatter(body_md or "")
    if fmt != "html":
        body = strip_duplicate_title(body, title)
        qa = render_qa_html(body, mapping)
        if qa is not None:
            return finish_html(qa, index, parent, attachments)
    rendered = str(render_body(body, mapping, fmt))
    return finish_html(rendered, index, parent, attachments)

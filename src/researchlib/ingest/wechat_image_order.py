"""Recover proven image positions without replacing archived Markdown prose.

Only an explicitly linked, account/article-bound local raw record can supply
positions. Both HTML representations and the existing Markdown must agree on
all visible non-whitespace text and the old image occurrences. HTML is never
copied into the article, and this does not authenticate remote image bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import markdown

from researchlib.textutil import split_frontmatter

_VOID = frozenset({"area", "base", "br", "col", "hr", "img", "input", "param", "source", "track", "wbr"})
_UNSAFE = frozenset(
    {"html", "head", "body", "script", "style", "iframe", "object", "embed", "form", "textarea", "picture", "source"}
)
_IMAGE_CONTEXT = frozenset(
    {
        "blockquote",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "pre",
        "code",
        "a",
        "figure",
        "figcaption",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "dl",
        "dt",
        "dd",
    }
)
_RAW_LINE = re.compile(r"(?m)^- 原始数据：\[[^\]\n]*\]\(([^)\n]+)\)[ \t]*\n[ \t]*\n")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\(([^)\n]+)\)")
_SN = re.compile(r"[a-fA-F0-9]{32}")
_MAX_RAW_BYTES = 2 * 1024 * 1024


class _Projection(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.images: list[tuple[str, int]] = []
        self.stack: list[str] = []
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        if tag in _UNSAFE:
            self.invalid = True
        if tag == "img":
            values = [value for key, value in attrs if key in {"src", "data-src"} and value]
            if not values or len(set(values)) != 1:
                self.invalid = True
            if any(parent in _IMAGE_CONTEXT for parent in self.stack):
                self.invalid = True
            if any(key == "srcset" and value for key, value in attrs):
                self.invalid = True
            self.images.append((values[0] if values else "", len(self.text)))
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.invalid = True
        if tag in self.stack:
            self.stack = self.stack[: len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, data):
        self.text += re.sub(r"\s+", "", data)


def _project(html: str) -> _Projection:
    result = _Projection()
    result.feed(html)
    result.close()
    result.invalid |= bool(result.stack)
    return result


def _url_identity(url: object) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or parts.hostname != "mp.weixin.qq.com"
        or parts.path != "/s"
        or parts.username
        or parts.password
        or port not in {None, 443}
    ):
        return None
    # A fragment selects a location within the same HTTP resource. It is not
    # sent to the server, used as identity evidence, or copied from raw HTML.
    sn, biz = query.get("sn"), query.get("__biz")
    if not sn or len(sn) != 1 or not _SN.fullmatch(sn[0]) or not biz or len(biz) != 1 or not biz[0]:
        return None
    return sn[0], biz[0]


def _contained_file(path: Path, owner: Path) -> bool:
    try:
        return (
            path.resolve().is_relative_to(owner.resolve())
            and path.is_file()
            and not any(part.is_symlink() for part in (path, *path.parents))
        )
    except (OSError, RuntimeError):
        return False


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocked = []
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag in {"pre", "code", "script", "style", "textarea"}:
            self.blocked.append(tag)
        elif tag == "a" and not self.blocked:
            self.hrefs.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag):
        if tag in self.blocked:
            self.blocked = self.blocked[: len(self.blocked) - 1 - self.blocked[::-1].index(tag)]


def restore_image_order(original: str, path: Path, roots: list[Path]) -> tuple[str, dict]:
    """Return original prose plus proven image moves, or an unchanged review flag.

    Call only for the wechat_mp importer. Ordinal filenames, exact old offsets
    and complete URI occurrence order preserve the existing archive's media
    binding; they do not certify image contents against a remote server.
    """

    def fallback(reason, status="needs_review"):
        return original, {"status": status, "reason": reason}

    front, _ = split_frontmatter(original)
    sn, account = front.get("sn"), front.get("ghid")
    if not isinstance(sn, str) or not _SN.fullmatch(sn) or not isinstance(account, str) or not account:
        return fallback("missing_article_account_identity", "no_evidence")
    links = list(_RAW_LINE.finditer(original))
    if len(links) != 1:
        return fallback("missing_or_ambiguous_explicit_raw_link", "no_evidence" if not links else "needs_review")
    match = links[0]
    _, header = split_frontmatter(original[: match.start()])
    allowed = ("- 作者：", "- 时间：", "- sn：", "- 原链接：")
    if any(
        line.strip() != "# " + str(front.get("title") or "") and not line.strip().startswith(allowed)
        for line in header.splitlines()
        if line.strip()
    ):
        return fallback("unsupported_export_header")
    rendered_links = _Links()
    rendered_links.feed(markdown.markdown(split_frontmatter(original)[1], extensions=["extra"]))
    raw_links = [href for href in rendered_links.hrefs if "raw" in href and ".json" in href]
    if raw_links != [match.group(1)]:
        return fallback("unverified_or_ambiguous_raw_link")
    try:
        href = urlsplit(match.group(1))
        dest = unquote(href.path).replace("\\", "/")
        relative = Path(dest)
        owner = max(
            (root for root in roots if path.resolve().is_relative_to(root.resolve())),
            key=lambda root: len(root.resolve().parts),
        )
    except (OSError, RuntimeError, ValueError):
        return fallback("unbound_archive_root")
    if (
        href.scheme
        or href.netloc
        or href.query
        or href.fragment
        or relative.is_absolute()
        or ":" in dest
        or "\x00" in dest
        or "raw" not in relative.parts
        or relative.name != sn + ".json"
    ):
        return fallback("unsafe_or_mismatched_raw_link")
    raw_path = path.parent / relative
    try:
        if not _contained_file(raw_path, owner) or raw_path.stat().st_size > _MAX_RAW_BYTES:
            return fallback("raw_missing_unsafe_or_too_large")
        raw_bytes = raw_path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return fallback("invalid_raw_record")
    if not isinstance(raw, dict):
        return fallback("invalid_raw_record")
    identity = _url_identity(raw.get("url"))
    if (
        identity is None
        or identity != _url_identity(front.get("url"))
        or identity[0] != sn
        or raw.get("user_name") != account
        or raw.get("title") != front.get("title")
        or (raw.get("sn") is not None and raw["sn"] != sn)
        or (raw.get("ghid") is not None and raw["ghid"] != account)
        or (raw.get("biz") is not None and raw["biz"] != identity[1])
    ):
        return fallback("raw_identity_conflict")
    author = raw.get("author") or raw.get("nick_name")
    if front.get("author") and author and front["author"] != author:
        return fallback("raw_author_conflict")
    content, rich = raw.get("content"), raw.get("content_multi_text")
    if not isinstance(content, str) or not isinstance(rich, str) or not content.strip() or not rich.strip():
        return fallback("missing_or_empty_html_field", "no_evidence")
    plain_html, rich_html = _project(content), _project(rich)
    if plain_html.invalid or rich_html.invalid:
        return fallback("unsafe_or_incomplete_html")
    article = original[match.end() :]
    md = _project(markdown.markdown(article, extensions=["extra"]))
    if md.invalid or not md.text or md.text != plain_html.text or md.text != rich_html.text:
        return fallback("visible_text_disagreement")
    old_uris = [uri for uri, _ in plain_html.images]
    if not old_uris or old_uris != [uri for uri, _ in rich_html.images]:
        return fallback("image_identity_or_occurrence_disagreement")
    if [offset for _, offset in md.images] != [offset for _, offset in plain_html.images]:
        return fallback("old_image_position_disagreement")
    # A unit owns its preceding blank separator. Reordering image units leaves
    # every non-image unit, including its Markdown syntax, byte-for-byte intact.
    parts = re.split(r"(\n[ \t]*\n+)", article)
    units = [("", parts[0])] + [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]
    image_units, prose = [], []
    for separator, block in units:
        image = _IMAGE.fullmatch(block.strip())
        if image:
            image_units.append((separator, block, image.group(1)))
        else:
            if _IMAGE.search(block) or re.search(r"<img\b|^\s*(```|~~~)", block, re.I | re.M):
                return fallback("unsupported_markdown_image_structure")
            if block.strip():
                prose.append((separator, block))
            elif separator or block:
                return fallback("empty_or_ambiguous_paragraph")
    if len(image_units) != len(old_uris) or len(md.images) != len(image_units):
        return fallback("image_count_disagreement")
    for number, (_, _, ref) in enumerate(image_units, 1):
        try:
            target = urlsplit(ref)
        except ValueError:
            return fallback("unsafe_local_image")
        local = path.parent / unquote(target.path)
        if (
            target.scheme
            or target.netloc
            or target.query
            or target.fragment
            or Path(target.path).is_absolute()
            or local.stem != f"{sn}_{number:02d}"
            or local.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
            or not _contained_file(local, owner)
        ):
            return fallback("unbound_local_image_occurrence")
    boundaries = {0: 0}
    offset = 0
    for index, (_, block) in enumerate(prose, 1):
        value = _project(markdown.markdown(block, extensions=["extra"]))
        if value.invalid or not value.text:
            return fallback("empty_or_ambiguous_paragraph")
        offset += len(value.text)
        if offset in boundaries:
            return fallback("empty_or_ambiguous_paragraph")
        boundaries[offset] = index
    if offset != len(md.text):
        return fallback("markdown_context_disagreement")
    positions = [position for _, position in rich_html.images]
    if any(position not in boundaries for position in positions):
        return fallback("image_position_inside_markdown_paragraph")
    status = {
        "status": "unchanged",
        "reason": "matching_positions",
        "evidence": "content_multi_text",
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "image_occurrences": len(image_units),
        "binding": "explicit_article_account_link_and_old_ordered_image_offsets",
    }
    if positions == [position for _, position in md.images]:
        return original, status
    if any(not separator for separator, _, _ in image_units):
        return fallback("unsupported_leading_image_boundary")
    insertions: dict[int, list[str]] = {}
    for (separator, block, _), position in zip(image_units, positions, strict=True):
        # At boundary zero the image's own separator follows it, keeping the
        # first prose unit untouched and preventing an image/prose join.
        unit = block + separator if position == 0 else separator + block
        insertions.setdefault(boundaries[position], []).append(unit)
    rebuilt = "".join(insertions.get(0, [])) + "".join(
        separator + block + "".join(insertions.get(index, [])) for index, (separator, block) in enumerate(prose, 1)
    )
    result = _project(markdown.markdown(rebuilt, extensions=["extra"]))
    if (
        result.invalid
        or result.text != md.text
        or [position for _, position in result.images] != positions
        or [uri for uri, _ in result.images] != [uri for uri, _ in md.images]
    ):
        return fallback("reconstruction_disagreement")
    status.update(status="restored", reason="matching_text_and_proven_paragraph_boundaries")
    return original[: match.end()] + rebuilt, status

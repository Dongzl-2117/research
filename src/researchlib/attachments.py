"""归档附件的显式映射。只在导入时复制文件，阅读和下载路径均不写入。"""

from __future__ import annotations

import html as html_lib
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from researchlib.images import sha256_file

ATTACHMENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".epub": "application/epub+zip",
    ".zip": "application/zip",
}
_SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_FILE = re.compile(r"([0-9a-f]{64})(\.[a-z0-9]+)\Z")
_MAX_BYTES = 100 * 1024 * 1024
_A_TAG = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF = re.compile(r'''\bhref\s*=\s*(["'])(.*?)\1''', re.I | re.S)
_DOWNLOAD = re.compile(r"/articles/[0-9]+/attachments/[0-9a-f]{64}\.[a-z0-9]+\Z")
_MD_LINK = re.compile(
    r'''(?<!!)\[([^\]\n]*)\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))(?:[ \t]+(?:"[^"]*"|'[^']*'))?[ \t]*\)'''
)


def filter_attachment_manifest(meta: dict, body_md: str) -> dict:
    """Only links in current rendered content authorize attachment access."""
    import markdown

    class Links(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.blocked = []
            self.refs = set()

        def handle_starttag(self, tag, attrs):
            if tag in {"code", "pre", "script", "style", "textarea"}:
                self.blocked.append(tag)
            elif tag == "a" and not self.blocked:
                ref = relative_attachment_path(dict(attrs).get("href") or "")
                if ref is not None:
                    self.refs.add(ref)

        def handle_endtag(self, tag):
            if tag in self.blocked:
                index = len(self.blocked) - 1 - self.blocked[::-1].index(tag)
                del self.blocked[index:]

    parsed = Links()
    parsed.feed(markdown.markdown(body_md or "", extensions=["extra"]))
    parsed.close()
    result = dict(meta or {})
    manifest = result.pop("local_attachments", None)
    if isinstance(manifest, dict):
        kept = {ref: item for ref, item in manifest.items() if ref in parsed.refs}
        if kept:
            result["local_attachments"] = kept
    return result


def relative_attachment_path(href: str) -> str | None:
    """附件相对路径；不接受绝对路径、URL、活动内容或未知扩展名。"""
    raw = html_lib.unescape(str(href or "").strip().strip("<>"))
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if not raw or parts.scheme or parts.netloc or raw.startswith(("/", "\\", "#", "?")):
        return None
    path = unquote(parts.path).replace("\\", "/")
    if not path or "\x00" in path or path.startswith("/") or ":" in path:
        return None
    if Path(path).suffix.lower() not in ATTACHMENT_TYPES:
        return None
    return path


def _contained_file(path: Path, roots: list[Path]) -> Path | None:
    try:
        resolved = path.resolve()
        owner = next((root for root in roots if resolved.is_relative_to(root.resolve())), None)
        if owner is None or not resolved.is_file() or resolved.stat().st_size > _MAX_BYTES:
            return None
        # resolve() 本身会跟随符号链接；必须同时检查原路径各层。
        relative = path.absolute().relative_to(owner.absolute())
        pieces = [owner / Path(*relative.parts[:n]) for n in range(len(relative.parts) + 1)]
        if any(part.is_symlink() for part in pieces):
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def collect_attachment_refs(text: str, md_path: Path, roots: list[Path]) -> list[tuple[str, Path | None]]:
    from researchlib.mdlinks import iter_link_targets

    refs: list[tuple[str, Path | None]] = []
    seen: set[str] = set()
    for href in iter_link_targets(text):
        path = relative_attachment_path(href)
        if path is None or path in seen:
            continue
        seen.add(path)
        candidates = [md_path.parent / path, *(root / path for root in roots)]
        matches = {_contained_file(candidate, roots) for candidate in candidates}
        matches.discard(None)
        # 文件名相同不能猜；仅使用实际相对路径。
        refs.append((path, next(iter(matches)) if len(matches) == 1 else None))
    return refs


def with_stored_attachments(
    raw_meta: dict,
    refs: list[tuple[str, Path | None]],
    data_dir: Path,
    source_id: str,
) -> dict:
    """导入专用：返回附件清单，绝不由 GET 调用。"""
    meta = dict(raw_meta or {})
    meta.pop("local_attachments", None)
    if not _SOURCE.fullmatch(source_id):
        return meta
    manifest = {}
    for ref, path in refs:
        if relative_attachment_path(ref) != ref or path is None or path.is_symlink() or not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in ATTACHMENT_TYPES or path.stat().st_size > _MAX_BYTES:
            continue
        if ext == ".pdf":
            with path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    continue
        digest = sha256_file(path)
        storage = f"{source_id}/{digest[:2]}/{digest}{ext}"
        root = data_dir / "attachments"
        target = root / storage
        if any(part.is_symlink() for part in [root, target.parent.parent, target.parent, target]):
            continue
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        manifest[ref] = {
            "storage_path": storage,
            "name": Path(ref).name,
            "sha256": digest,
            "media_type": ATTACHMENT_TYPES[ext],
        }
    if manifest:
        meta["local_attachments"] = manifest
    return meta


def _manifest_entries(meta: dict, source_id: str):
    if not _SOURCE.fullmatch(source_id):
        return
    manifest = (meta or {}).get("local_attachments")
    if not isinstance(manifest, dict):
        return
    for ref, item in manifest.items():
        if not isinstance(ref, str) or relative_attachment_path(ref) != ref or not isinstance(item, dict):
            continue
        storage = str(item.get("storage_path") or "")
        parts = storage.split("/")
        if len(parts) != 3 or parts[0] != source_id:
            continue
        match = _FILE.fullmatch(parts[2])
        if match is None or parts[1] != match.group(1)[:2]:
            continue
        digest, ext = match.groups()
        if ext not in ATTACHMENT_TYPES or item.get("sha256") != digest:
            continue
        yield ref, item, storage, parts[2], ATTACHMENT_TYPES[ext]


def attachment_mapping(meta: dict, article_id: int, source_id: str) -> dict[str, str]:
    return {
        ref: f"/articles/{int(article_id)}/attachments/{filename}"
        for ref, _item, _storage, filename, _mime in _manifest_entries(meta, source_id)
    }


def resolve_article_attachment(
    data_dir: Path, source_id: str, meta: dict, filename: str, *, body_md: str | None = None
) -> tuple[Path, str, str] | None:
    if _FILE.fullmatch(filename) is None:
        return None
    if body_md is not None:
        meta = filter_attachment_manifest(meta, body_md)
    for _ref, item, storage, candidate, mime in _manifest_entries(meta, source_id):
        if candidate != filename:
            continue
        root = data_dir / "attachments"
        target = _contained_file(root / storage, [root])
        if target is None:
            return None
        name = str(item.get("name") or filename).replace("\r", "").replace("\n", "")
        name = name.replace("\\", "/").rsplit("/", 1)[-1] or filename
        return target, mime, name
    return None


def rewrite_relative_attachment_links(html: str, mapping: dict[str, str] | None = None) -> str:
    def replace(match: re.Match[str]) -> str:
        attrs, label = match.group(1), match.group(2)
        href = _HREF.search(attrs)
        if href is None:
            return match.group(0)
        path = relative_attachment_path(href.group(2))
        if path is None:
            return match.group(0)
        target = (mapping or {}).get(path)
        if target is None or _DOWNLOAD.fullmatch(target) is None:
            return f'{label}<span class="doc-missing">附件未收录</span>'
        safe_target = html_lib.escape(target, quote=True)
        attrs = _HREF.sub(lambda _m: f'href="{safe_target}"', attrs, count=1)
        return f'<a{attrs} download>{label}</a>'

    return _A_TAG.sub(replace, html or "")


def rewrite_markdown_attachment_links(text: str, mapping: dict[str, str] | None = None) -> str:
    """API Markdown 使用与 HTML 相同的附件映射，代码、图片和外链保持原样。"""
    def rewrite(block: str) -> str:
        code = []

        def protect(match: re.Match[str]) -> str:
            code.append(match.group(0))
            return f"\x00ATTACHMENTCODE{len(code) - 1}\x00"

        block = re.sub(r"(`+)([^`]*?)\1", protect, block)

        def replace(match: re.Match[str]) -> str:
            label, angle, bare = match.groups()
            path = relative_attachment_path(angle or bare)
            if path is None:
                return match.group(0)
            target = (mapping or {}).get(path)
            if target is None or _DOWNLOAD.fullmatch(target) is None:
                return f"{label}（附件未收录）"
            return f"[{label}]({target})"

        block = _MD_LINK.sub(replace, block)
        missing = set()

        def reference(match: re.Match[str]) -> str:
            label, angle, bare = match.groups()
            path = relative_attachment_path(angle or bare)
            if path is None:
                return match.group(0)
            target = (mapping or {}).get(path)
            if target is None or _DOWNLOAD.fullmatch(target) is None:
                missing.add(" ".join(label.casefold().split()))
                return ""
            return f"[{label}]: {target}"

        block = re.sub(r"(?m)^ {0,3}\[([^\]\n]+)\]:[ \t]*(?:<([^>\n]+)>|([^\s\n]+))[^\n]*$", reference, block)

        def missing_reference(match: re.Match[str]) -> str:
            label, reference_id = match.groups()
            key = " ".join((reference_id or label).casefold().split())
            return f"{label}（附件未收录）" if key in missing else match.group(0)

        block = re.sub(r"(?<!!)\[([^\]\n]+)\]\[([^\]\n]*)\]", missing_reference, block)
        block = re.sub(
            r"(?<!!)\[([^\]\n]+)\](?![\[(])",
            lambda match: (f"{match.group(1)}（附件未收录）"
                           if " ".join(match.group(1).casefold().split()) in missing else match.group(0)),
            block,
        )
        for index, value in enumerate(code):
            block = block.replace(f"\x00ATTACHMENTCODE{index}\x00", value)
        return block

    out = []
    plain = []
    fence = ""
    for line in (text or "").splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            if plain:
                out.append(rewrite("".join(plain)))
                plain = []
            token = marker.group(1)[0]
            if not fence:
                fence = token
            elif token == fence:
                fence = ""
            out.append(line)
        elif fence:
            out.append(line)
        else:
            plain.append(line)
    out.append(rewrite("".join(plain)))
    return "".join(out)

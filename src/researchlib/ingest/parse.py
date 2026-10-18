"""从 Markdown 树、微信压缩包或知识星球目录解析文章。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from researchlib.ingest.archive import ImageIndex, iter_files, materialize
from researchlib.ingest.model import ParsedDoc
from researchlib.render import image_destination
from researchlib.textutil import (
    basename,
    date_from_filename,
    derive_title,
    normalize_newlines,
    parse_datetime,
    split_frontmatter,
)

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HTML_IMAGE = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_TOPIC_ID = re.compile(r"\d{5,}")


def read_text_file_safe(path: Path) -> str:
    data = path.read_bytes().replace(b"\x00", b"")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def image_refs_in(text: str) -> list[str]:
    refs = [match.group(2) for match in _MD_IMAGE.finditer(text)]
    refs.extend(match.group(1) for match in _HTML_IMAGE.finditer(text))
    return refs


def relative_id(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = Path(basename(path.name))
    return rel.as_posix()


def common_root(paths: list[Path]) -> Path:
    if not paths:
        return Path(".")
    parts = [path.parent.resolve().parts for path in paths]
    shared: list[str] = []
    for column in zip(*parts, strict=False):
        if len(set(column)) != 1:
            break
        shared.append(column[0])
    if not shared:
        return paths[0].parent
    return Path(*shared)


def _frontmatter_id(frontmatter: dict) -> str | None:
    for key in ("id", "article_id", "topic_id", "msgid"):
        value = frontmatter.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _frontmatter_date(frontmatter: dict):
    for key in ("date", "published_at", "published", "pub_date", "created", "create_time"):
        if key in frontmatter:
            parsed = parse_datetime(frontmatter.get(key))
            if parsed:
                return parsed
    return None


def extract_zsxq_meta(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    nodes = [data]
    for key in ("resp_data", "data", "topic", "result"):
        child = data.get(key)
        if isinstance(child, dict):
            nodes.append(child)
    found: dict = {}
    for node in nodes:
        if "topic_id" not in found:
            for key in ("topic_id", "topicId", "id"):
                if node.get(key) not in (None, ""):
                    found["topic_id"] = str(node[key])
                    break
        if "create_time" not in found:
            for key in ("create_time", "createTime", "created_at", "publish_time"):
                if node.get(key) not in (None, ""):
                    found["create_time"] = node[key]
                    break
        if "title" not in found and isinstance(node.get("title"), str) and node["title"].strip():
            found["title"] = node["title"].strip()
        talk = node.get("talk")
        if isinstance(talk, dict) and "title" not in found:
            if isinstance(talk.get("title"), str) and talk["title"].strip():
                found["title"] = talk["title"].strip()
    return found


def load_raw_index(raw_files: list[Path]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for path in raw_files:
        try:
            data = json.loads(read_text_file_safe(path))
        except json.JSONDecodeError:
            continue
        meta = extract_zsxq_meta(data)
        index.setdefault(path.stem, meta)
        topic_id = meta.get("topic_id")
        if topic_id:
            index.setdefault(str(topic_id), meta)
    return index


def _match_raw(path: Path, index: dict[str, dict]) -> dict:
    if path.stem in index:
        return index[path.stem]
    for topic_id in _TOPIC_ID.findall(path.stem):
        if topic_id in index:
            return index[topic_id]
    return {}


def _dedupe_ids(docs: list[ParsedDoc], errors: list[str]) -> None:
    seen: set[str] = set()
    for doc in docs:
        external_id = doc.external_id
        if external_id not in seen:
            seen.add(external_id)
            continue
        suffix = 2
        while f"{external_id}-{suffix}" in seen:
            suffix += 1
        new_id = f"{external_id}-{suffix}"
        errors.append(f"外部 id 重复，{external_id} 改成 {new_id}")
        doc.external_id = new_id
        seen.add(new_id)


def parse_markdown_files(
    files: list[Path],
    *,
    roots: list[Path],
    images: ImageIndex,
    id_mode: str,
    raw_index: dict[str, dict] | None = None,
) -> tuple[list[ParsedDoc], list[str]]:
    docs: list[ParsedDoc] = []
    errors: list[str] = []
    text_root = common_root(files)
    for path in files:
        try:
            original = read_text_file_safe(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        frontmatter, body = split_frontmatter(original)
        body = normalize_newlines(body)
        raw_meta = _match_raw(path, raw_index or {})
        title = derive_title(frontmatter, body, path.name, raw_meta.get("title"))
        published = _frontmatter_date(frontmatter) or parse_datetime(raw_meta.get("create_time"))
        if published is None:
            published = date_from_filename(path.name)
        if id_mode == "path":
            root = _owning_root(path, roots) or text_root
            external_id = _frontmatter_id(frontmatter) or relative_id(path, root)
            parent = str(Path(external_id).parent.as_posix())
            if parent == ".":
                parent = ""
        elif id_mode == "zsxq":
            external_id = str(raw_meta.get("topic_id") or _frontmatter_id(frontmatter) or path.stem)
            parent = ""
        else:
            external_id = _frontmatter_id(frontmatter) or relative_id(path, text_root)
            parent = str(Path(external_id).parent.as_posix())
            if parent == ".":
                parent = ""
        refs: list[tuple[str, Path | None]] = []
        seen_refs: set[str] = set()
        for ref in image_refs_in(original):
            dest = image_destination(ref)
            if not dest or dest.startswith(("http://", "https://", "//", "data:")):
                continue
            if dest in seen_refs:
                continue
            seen_refs.add(dest)
            refs.append((dest, images.resolve(ref, path)))
        full_md = normalize_newlines(original)
        docs.append(
            ParsedDoc(
                external_id=external_id[:500],
                title=title,
                published_at=published,
                body_md=full_md,
                raw_meta={
                    "format": "markdown",
                    "parent": parent,
                    "filename": path.name,
                    "topic_id": raw_meta.get("topic_id"),
                },
                image_refs=refs,
            )
        )
    _dedupe_ids(docs, errors)
    return docs, errors


def _owning_root(path: Path, roots: list[Path]) -> Path | None:
    resolved = path.resolve()
    owners = []
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        owners.append(root)
    if not owners:
        return None
    owners.sort(key=lambda item: len(item.resolve().parts), reverse=True)
    return owners[0]


def parse_archive(input_path: Path, work: Path, *, id_mode: str) -> tuple[list[ParsedDoc], list[str], int, int]:
    roots = materialize(input_path, work)
    markdown, images, raw_json = iter_files(roots)
    if id_mode == "zsxq":
        md_dirs = [root / "md" for root in roots if (root / "md").is_dir()]
        if md_dirs:
            chosen = []
            for directory in md_dirs:
                chosen.extend(
                    path
                    for path in directory.rglob("*")
                    if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".md", ".markdown"}
                )
            if chosen:
                markdown = chosen
        raw_dirs = [path for path in raw_json if path.parent.name == "raw"]
        raw_index = load_raw_index(raw_dirs or raw_json)
    else:
        raw_index = {}
    index = ImageIndex(images, roots)
    docs, errors = parse_markdown_files(
        markdown,
        roots=roots,
        images=index,
        id_mode=id_mode,
        raw_index=raw_index,
    )
    return docs, errors, len(markdown), len(images)

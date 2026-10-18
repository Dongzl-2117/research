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
    derive_title,
    normalize_newlines,
    resolve_published_at,
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


def _first_value(node: dict, keys: tuple[str, ...]):
    for key in keys:
        value = node.get(key)
        if value not in (None, ""):
            return value
    return None


def _iter_dicts(data, depth: int = 0):
    if depth > 8:
        return
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _iter_dicts(value, depth + 1)
    elif isinstance(data, list):
        for item in data[:50]:
            yield from _iter_dicts(item, depth + 1)


def _topic_title(node: dict) -> str | None:
    title = node.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for key in ("talk", "question", "task", "solution"):
        child = node.get(key)
        if not isinstance(child, dict):
            continue
        child_title = child.get("title")
        if isinstance(child_title, str) and child_title.strip():
            return child_title.strip()
    return None


def _topic_time(node: dict):
    found = _first_value(node, ("create_time", "createTime", "created_at", "publish_time", "published_at"))
    if found not in (None, ""):
        return found
    for key in ("topic", "talk", "question"):
        child = node.get(key)
        if isinstance(child, dict):
            found = _first_value(child, ("create_time", "createTime", "created_at", "publish_time"))
            if found not in (None, ""):
                return found
    return None


def extract_zsxq_topics(data: object) -> list[dict]:
    """一个 JSON 里可能是单条话题，也可能是嵌套的 topics 列表。"""
    topics: list[dict] = []
    seen: set[str] = set()
    for node in _iter_dicts(data):
        topic_id = _first_value(node, ("topic_id", "topicId"))
        if topic_id in (None, ""):
            continue
        topic_id = str(topic_id)
        if topic_id in seen:
            continue
        seen.add(topic_id)
        topics.append(
            {
                "topic_id": topic_id,
                "create_time": _topic_time(node),
                "title": _topic_title(node),
            }
        )
    if topics:
        return topics
    if not isinstance(data, dict):
        return []
    merged_time = None
    merged_title = None
    for node in _iter_dicts(data):
        if merged_time is None:
            merged_time = _topic_time(node)
        if merged_title is None:
            merged_title = _topic_title(node)
    if merged_time is None and merged_title is None:
        return []
    return [{"topic_id": None, "create_time": merged_time, "title": merged_title}]


def extract_zsxq_meta(data: object) -> dict:
    topics = extract_zsxq_topics(data)
    return topics[0] if topics else {}


def load_raw_index(raw_files: list[Path]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for path in raw_files:
        try:
            data = json.loads(read_text_file_safe(path))
        except json.JSONDecodeError:
            continue
        metas = extract_zsxq_topics(data)
        if not metas:
            continue
        index.setdefault(path.stem, metas[0])
        for meta in metas:
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
        published, date_source = resolve_published_at(
            frontmatter=frontmatter,
            hints=raw_meta,
            filename=path.name,
            text=original,
        )
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
        stored_meta: dict = {
            "format": "markdown",
            "parent": parent,
            "filename": path.name,
        }
        if raw_meta.get("topic_id"):
            stored_meta["topic_id"] = raw_meta.get("topic_id")
        create_time = raw_meta.get("create_time")
        if create_time not in (None, ""):
            if isinstance(create_time, (str, int, float, bool)):
                stored_meta["create_time"] = create_time
            else:
                stored_meta["create_time"] = str(create_time)
        if date_source:
            stored_meta["date_source"] = date_source
        docs.append(
            ParsedDoc(
                external_id=external_id[:500],
                title=title,
                published_at=published,
                body_md=full_md,
                raw_meta=stored_meta,
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

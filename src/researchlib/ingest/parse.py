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


_ZSXQ_TAG = re.compile(r"<e\b([^>]*)/?>", re.I)
_ZSXQ_ATTR = re.compile(r"""(\w+)\s*=\s*["']([^"']*)["']""")
_ANY_TAG = re.compile(r"<[^>]+>")
_KEEP_CHARS = re.compile(r"[0-9A-Za-z一-鿿]+")
# 知识星球导入时接在正文后面的一节。重新导入时只有这一节变了（多了回复）不算改稿，见 revisions.split_comments。
COMMENTS_MARKER = "\n\n## 评论里的回复\n\n"


def zsxq_text(raw: object) -> str:
    """知识星球正文和评论里的标记：话题、链接换成它们的文字，@某人去掉（不留其他读者的名字）。"""
    import html as html_lib
    from urllib.parse import unquote

    def replace(match: re.Match[str]) -> str:
        attrs = {key.lower(): value for key, value in _ZSXQ_ATTR.findall(match.group(1))}
        if attrs.get("type") == "mention":
            return ""
        return unquote(attrs.get("title") or "")

    text = _ZSXQ_TAG.sub(replace, str(raw or ""))
    text = _ANY_TAG.sub("", text)
    return html_lib.unescape(text).strip()


def _poster(node: dict) -> dict:
    """这条话题是谁发的。问答看回答的人（星主），普通帖看 talk 的作者（可能是网友）。"""
    for key in ("talk", "answer", "task", "solution"):
        child = node.get(key)
        if isinstance(child, dict) and isinstance(child.get("owner"), dict):
            owner = child["owner"]
            name = str(owner.get("name") or "").strip()
            user_id = owner.get("user_id")
            if name or user_id not in (None, ""):
                return {
                    "poster_name": name,
                    "poster_id": "" if user_id in (None, "") else str(user_id),
                }
    owner = node.get("owner")
    if isinstance(owner, dict):
        name = str(owner.get("name") or "").strip()
        user_id = owner.get("user_id")
        if name or user_id not in (None, ""):
            return {
                "poster_name": name,
                "poster_id": "" if user_id in (None, "") else str(user_id),
            }
    return {}


def _owner_id(node: dict) -> str | None:
    """这条话题是谁发的：普通话题看 talk 的作者，问答看回答的人（星主）。"""
    for key in ("talk", "answer", "task", "solution"):
        child = node.get(key)
        if isinstance(child, dict) and isinstance(child.get("owner"), dict):
            user = child["owner"].get("user_id")
            if user not in (None, ""):
                return str(user)
    owner = node.get("owner")
    if isinstance(owner, dict) and owner.get("user_id") not in (None, ""):
        return str(owner["user_id"])
    return None


def _comments(node: dict) -> list[dict]:
    """话题下的评论，楼中楼摊平，按原来的顺序。"""
    found: list[dict] = []
    for key in ("show_comments", "comments"):
        items = node.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            found.append(item)
            for reply in item.get("replied_comments") or []:
                if isinstance(reply, dict):
                    found.append(reply)
        if found:
            break
    return found


def _user(comment: dict, key: str) -> str | None:
    value = comment.get(key)
    if isinstance(value, dict) and value.get("user_id") not in (None, ""):
        return str(value["user_id"])
    return None


def author_replies(node: dict) -> list[tuple[str, str]]:
    """作者本人在评论里的回复：[(被回复的那条读者评论, 作者的回复)]，没有被回复的那条就是空串。"""
    owner = _owner_id(node)
    if owner is None:
        return []
    comments = _comments(node)
    pairs: list[tuple[str, str]] = []
    for index, comment in enumerate(comments):
        if _user(comment, "owner") != owner:
            continue
        reply = zsxq_text(comment.get("text"))
        if not reply:
            continue
        question = ""
        target = _user(comment, "repliee")
        if target and target != owner:
            for earlier in reversed(comments[:index]):
                if _user(earlier, "owner") == target:
                    question = zsxq_text(earlier.get("text"))
                    break
        pairs.append((question, reply))
    return pairs


def comments_section(pairs: list[tuple[str, str]], body: str) -> str:
    """接在正文后面的「评论里的回复」。正文里已经有的回复（导出时带上了评论）不重复。"""
    seen = "".join(_KEEP_CHARS.findall(body or ""))
    lines: list[str] = []
    for question, reply in pairs:
        key = "".join(_KEEP_CHARS.findall(reply))[:30]
        if not key or key in seen:
            continue
        seen += key
        if question:
            lines.append(f"读者问：{question}")
        lines.append(f"作者答：{reply}" if question else f"作者补充：{reply}")
    if not lines:
        return ""
    return COMMENTS_MARKER + "\n\n".join(lines) + "\n"


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
                "replies": author_replies(node),
                **_poster(node),
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


def _keep_topic(index: dict[str, dict], key: str, meta: dict) -> None:
    current = index.get(key)
    if current is None or (current.get("create_time") in (None, "") and meta.get("create_time") not in (None, "")):
        index[key] = meta


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
        _keep_topic(index, path.stem, metas[0])
        for meta in metas:
            topic_id = meta.get("topic_id")
            if topic_id:
                _keep_topic(index, str(topic_id), meta)
    return index


def is_index_markdown(path: Path) -> bool:
    """知识星球导出的 INDEX.md 是目录，不是一条话题。"""
    return path.stem.lower() == "index" and path.suffix.lower() in {".md", ".markdown"}


def select_raw_json(paths: list[Path]) -> list[Path]:
    """话题 JSON。raw/topics/*.json 也要，不能只因为 raw/ 根上有 group.json、state.json 就停住。

    以前 `parent.name == "raw"` 的列表非空时就不再看子目录，create_time 全部丢成文件名零点。
    topics 里的单篇放前面，同一个 topic_id 先被收进索引。
    """
    under = [path for path in paths if "raw" in path.parts[:-1]]
    chosen = under or list(paths)

    def rank(path: Path) -> tuple[int, str]:
        return (0 if "topics" in path.parts else 1, path.as_posix())

    return sorted(chosen, key=rank)


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
        published, date_source, time_known = resolve_published_at(
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
        if id_mode == "zsxq" and raw_meta.get("replies"):
            # 星主在评论里回答读者的话往往比正文更直接：接在正文后面，进检索和问库。
            section = comments_section(raw_meta["replies"], full_md)
            if section:
                full_md = full_md.rstrip("\n") + "\n" + section
                stored_meta["comment_replies"] = section.count("作者答：") + section.count("作者补充：")
        if raw_meta.get("topic_id"):
            stored_meta["topic_id"] = raw_meta.get("topic_id")
        poster_name = str(raw_meta.get("poster_name") or "").strip()
        if poster_name:
            stored_meta["poster_name"] = poster_name
        poster_id = str(raw_meta.get("poster_id") or "").strip()
        if poster_id:
            stored_meta["poster_id"] = poster_id
        create_time = raw_meta.get("create_time")
        if create_time not in (None, ""):
            if isinstance(create_time, (str, int, float, bool)):
                stored_meta["create_time"] = create_time
            else:
                stored_meta["create_time"] = str(create_time)
        if date_source:
            stored_meta["date_source"] = date_source
        stored_meta["time_known"] = time_known
        # 段永平博客：没日期的不进按天；问答只把作者那一节交给观点和摘要。
        voice = frontmatter.get("author_voice")
        if isinstance(voice, str) and voice.strip():
            stored_meta["author_voice"] = voice.strip()
        if frontmatter.get("off_timeline") is True:
            stored_meta["off_timeline"] = True
        if frontmatter.get("off_views") is True:
            stored_meta["off_views"] = True
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
        markdown = [path for path in markdown if not is_index_markdown(path)]
        raw_index = load_raw_index(select_raw_json(raw_json))
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

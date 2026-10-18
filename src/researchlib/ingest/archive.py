"""展开 zip、收集 Markdown 和图片。挡住 zip 路径穿越，不跟随符号链接。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from researchlib.images import IMAGE_EXTS

MD_EXTS = frozenset({".md", ".markdown"})
_MAX_ENTRY_BYTES = 4 * 1024 * 1024 * 1024


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def safe_extract(zip_path: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or any(part.startswith("/") for part in parts):
                continue
            if info.file_size > _MAX_ENTRY_BYTES:
                continue
            target = (dest / name).resolve()
            if not _is_within(target, dest):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            extracted += 1
    return extracted


def _extract_tree(root: Path, work: Path, depth: int) -> list[Path]:
    """把 root 里的 zip 解到 work。最多再解一层嵌套 zip。"""
    roots = [root]
    if depth <= 0 or not root.exists():
        return roots
    zips = [
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".zip"
    ]
    for index, zip_path in enumerate(zips):
        target = work / f"{index:03d}-{zip_path.stem}"
        try:
            safe_extract(zip_path, target)
        except zipfile.BadZipFile:
            continue
        roots.append(target)
        if depth > 1:
            roots.extend(_extract_tree(target, target / "_nested", depth - 1)[1:])
    return roots


def materialize(input_path: Path, work: Path) -> list[Path]:
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        target = work / "root"
        safe_extract(input_path, target)
        return _extract_tree(target, work / "nested", 1)
    if input_path.is_dir():
        return _extract_tree(input_path, work / "zips", 2)
    raise FileNotFoundError(input_path)


def iter_files(roots: list[Path]) -> tuple[list[Path], list[Path], list[Path]]:
    markdown: list[Path] = []
    images: list[Path] = []
    raw_json: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            suffix = path.suffix.lower()
            if suffix in MD_EXTS:
                markdown.append(path)
            elif suffix in IMAGE_EXTS:
                images.append(path)
            elif suffix == ".json":
                raw_json.append(path)
    return markdown, images, raw_json


class ImageIndex:
    def __init__(self, images: list[Path], roots: list[Path]) -> None:
        self.roots = roots
        self.by_name: dict[str, list[Path]] = {}
        for image in images:
            self.by_name.setdefault(image.name.lower(), []).append(image)

    def resolve(self, ref: str, md_path: Path) -> Path | None:
        from researchlib.render import image_destination

        dest = image_destination(ref)
        if not dest or dest.startswith(("http://", "https://", "//", "data:")):
            return None
        candidates = [md_path.parent / dest]
        for root in self.roots:
            candidates.append(root / dest)
            candidates.append(root / "images" / Path(dest).name)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_file() and any(_is_within(resolved, root) for root in self.roots):
                return resolved
        matches = self.by_name.get(Path(dest).name.lower(), [])
        if len(matches) == 1:
            return matches[0]
        if not matches:
            return None
        md_parts = set(md_path.resolve().parts)
        matches.sort(key=lambda item: len(md_parts.intersection(item.resolve().parts)), reverse=True)
        return matches[0]

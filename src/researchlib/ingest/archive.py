"""展开 zip、收集 Markdown 和图片。挡住 zip 路径穿越，不跟随符号链接。"""

from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath

from researchlib.images import IMAGE_EXTS

MD_EXTS = frozenset({".md", ".markdown"})
_MAX_ENTRY_BYTES = 4 * 1024 * 1024 * 1024
# 一次导入（一个 zip 或一个目录里所有 zip，连嵌套的一起）最多解出这么多：超过就整次停下报错，不导一半。
# 现在最大的归档（Stratechery、星球）解开不到 3 GiB、几万个文件。
MAX_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
MAX_TOTAL_ENTRIES = 300_000
MAX_ZIPS = 500


class ArchiveTooLarge(ValueError):
    """解开的总量、文件数或 zip 个数超过上限。"""


class ExtractBudget:
    """一次导入共用的解包额度。"""

    def __init__(self, *, total_bytes: int = MAX_TOTAL_BYTES, entries: int = MAX_TOTAL_ENTRIES, zips: int = MAX_ZIPS):
        self.bytes_left = total_bytes
        self.entries_left = entries
        self.zips_left = zips

    def open_zip(self, path: Path) -> None:
        self.zips_left -= 1
        if self.zips_left < 0:
            raise ArchiveTooLarge(f"要解的 zip 超过 {MAX_ZIPS} 个（到 {path.name} 为止）")

    def entry(self, name: str) -> None:
        self.entries_left -= 1
        if self.entries_left < 0:
            raise ArchiveTooLarge(f"解出的文件超过 {MAX_TOTAL_ENTRIES} 个（到 {name} 为止）")

    def spend(self, size: int, name: str) -> None:
        self.bytes_left -= size
        if self.bytes_left < 0:
            raise ArchiveTooLarge(f"解开的总量超过 {MAX_TOTAL_BYTES // 1024**3} GiB（到 {name} 为止）")

# 归档自己的说明文件：README 讲这份归档怎么来的，INDEX 是归档里文章的目录。
# 公众号、星球、雪球、Stratechery 这类按篇的归档，一个文件就是平台上的一篇，不会叫这两个名字，
# 叫这两个名字的是抓取脚本、导出工具写给人看的说明，不导入。
ARCHIVE_NOTE_STEMS = frozenset({"readme", "index"})
# 文档树（飞书）一层目录自己的那一页可能就叫 index.md：这类来源不按上面的名字筛。
DOCUMENT_TREE_KINDS = frozenset({"feishu"})


def is_archive_note(name: str) -> bool:
    """README.md、INDEX.md（不分大小写，.markdown 也算，在哪一层都算）。"""
    path = PurePosixPath(str(name).replace("\\", "/"))
    return path.suffix.lower() in MD_EXTS and path.stem.lower() in ARCHIVE_NOTE_STEMS


def archive_note_names() -> list[str]:
    """说明文件的文件名（小写），库里按 raw_meta.filename 认。"""
    return sorted(stem + ext for stem in ARCHIVE_NOTE_STEMS for ext in MD_EXTS)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def safe_extract(zip_path: Path, dest: Path, budget: ExtractBudget | None = None) -> int:
    """解到 dest。绝对路径、``..``、解析后跑出 dest 的条目跳过；符号链接条目写成普通文件。

    单个条目按实际解出的字节数算，超过 4 GiB 的丢掉这一个；总量、文件数超过 ``budget`` 抛 ArchiveTooLarge。
    """
    budget = budget or ExtractBudget()
    budget.open_zip(zip_path)
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
            budget.entry(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(info) as src, target.open("wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    budget.spend(len(chunk), name)
                    if written > _MAX_ENTRY_BYTES:
                        break
                    out.write(chunk)
            if written > _MAX_ENTRY_BYTES:
                target.unlink(missing_ok=True)
                continue
            extracted += 1
    return extracted


def _extract_tree(root: Path, work: Path, depth: int, budget: ExtractBudget) -> list[Path]:
    """把 root 里的 zip 解到 work。最多再解一层嵌套 zip，全程共用 budget。"""
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
            safe_extract(zip_path, target, budget)
        except zipfile.BadZipFile:
            continue
        roots.append(target)
        if depth > 1:
            roots.extend(_extract_tree(target, target / "_nested", depth - 1, budget)[1:])
    return roots


def materialize(input_path: Path, work: Path, budget: ExtractBudget | None = None) -> list[Path]:
    budget = budget or ExtractBudget()
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        target = work / "root"
        safe_extract(input_path, target, budget)
        return _extract_tree(target, work / "nested", 1, budget)
    if input_path.is_dir():
        return _extract_tree(input_path, work / "zips", 2, budget)
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

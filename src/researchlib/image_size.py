"""阅读页正文里的图补上宽高，竖长的截图另标 ``data-shape="tall"``。

宽高从 ``/media`` 对应的本地文件读（Pillow 只读文件头，不解码整张图），按 EXIF 方向转正。
浏览器拿 ``width``/``height`` 先按比例占好位置，图滚到附近才下（``loading="lazy"``）时，
下面的字不再往下跳；CSS 里 ``width: auto; height: auto`` 照旧，图显示多大不由这两个属性定。

竖长截图（高至少是宽的 ``TALL_RATIO`` 倍）在阅读栏里按手机屏宽显示（site.css 里
``img[data-shape="tall"]`` 那条）：手机截图铺满 680px 的栏时，里面的字有正文两倍大。

文件名就是内容哈希，同一路径的尺寸不会变，读到的记在进程里；读不到的（还没下载的外链图、
SVG、坏图）不记，原样输出。只在交给模板前最后加，和 ``lazy_images`` 一样不进译制、对照核验和回执用的 HTML。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

TALL_RATIO = 1.25
_CACHE_LIMIT = 50_000
# EXIF Orientation 5～8 是转了 90 度存的：浏览器按转正后的方向显示，宽高对调。
_ROTATED = frozenset({5, 6, 7, 8})

_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_SRC = re.compile(r"""(?<![\w-])src\s*=\s*(["'])/media/([^"'?#]+)\1""", re.I)
_HAS_SIZE = re.compile(r"\s(?:width|height)\s*=", re.I)
_HAS_SHAPE = re.compile(r"\sdata-shape\s*=", re.I)

_sizes: dict[str, tuple[int, int]] = {}
_lock = threading.Lock()


def _media_file(root: Path, storage_path: str) -> Path | None:
    """``/media/`` 后面那段对应的文件（root 已经 resolve 过）；路径不规矩（..、绝对路径）或出了 root 就不认。"""
    relative = unquote(storage_path)
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith(("/", "\\")) or any(part in {"..", ""} for part in parts):
        return None
    target = root.joinpath(*parts)
    try:
        if not target.resolve().is_relative_to(root):
            return None
    except OSError:
        return None
    return target


def image_size(path: Path) -> tuple[int, int] | None:
    """显示时的（宽，高），按 EXIF 方向转正；读不出来回 None。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
            orientation = image.getexif().get(0x0112)
    except Exception:  # noqa: BLE001 - 缺文件、SVG、坏图都只是不补尺寸
        return None
    if orientation in _ROTATED:
        width, height = height, width
    if width <= 0 or height <= 0:
        return None
    return width, height


def _media_size(root: Path, storage_path: str) -> tuple[int, int] | None:
    key = f"{root}\n{storage_path}"
    known = _sizes.get(key)
    if known is not None:
        return known
    path = _media_file(root, storage_path)
    size = image_size(path) if path is not None else None
    if size is None:
        return None
    with _lock:
        if len(_sizes) >= _CACHE_LIMIT:
            _sizes.clear()
        _sizes[key] = size
    return size


def size_images(html: str, media_root: Path) -> str:
    """给 ``src="/media/…"`` 的图补 ``width``/``height``，竖长的加 ``data-shape="tall"``。

    已经写了宽或高的不改那两个属性（原文自带的），只按文件实际比例判断是不是竖长图。可以重复跑。
    """
    if not html or "<img" not in html.lower():
        return html or ""
    try:
        root = media_root.resolve()
    except OSError:
        return html

    def add(match: re.Match[str]) -> str:
        tag = match.group(0)
        src = _SRC.search(tag)
        size = _media_size(root, src.group(2)) if src is not None else None
        if size is None:
            return tag
        width, height = size
        extra = ""
        if not _HAS_SIZE.search(tag):
            extra += f' width="{width}" height="{height}"'
        if height >= width * TALL_RATIO and not _HAS_SHAPE.search(tag):
            extra += ' data-shape="tall"'
        if not extra:
            return tag
        closing = " />" if tag.endswith("/>") else ">"
        head = tag[: -len(closing.strip())].rstrip()
        return f"{head}{extra}{closing}"

    return _IMG_TAG.sub(add, html)

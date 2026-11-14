"""/static 下的样式和脚本带内容版本号，浏览器和 Cloudflare 可以一直缓存。

页面统一回 ``Cache-Control: private, no-store``（app.privacy_headers）。以前样式和脚本也这样：
每打开一页都要从 Mac mini 经隧道重下一遍 site.css（85KB）和问库的脚本，样式没到页面就出不来。
模板里写 ``{{ asset('site.css') }}``，得到 ``/static/site.css?v=<内容哈希>``；带着当前版本号来取的，
回一年的 immutable 缓存。改了文件版本号就变，新页面取新地址；版本号对不上的照旧不缓存。
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"
# /media 的图片：只许读者自己的浏览器缓存（private），Cloudflare 不存。见 hashed_media。
MEDIA_CACHE = "private, max-age=31536000, immutable"
# 站标一天取一次就够；改了图标最多一天后换上。
FAVICON_CACHE = "public, max-age=86400"
_HASHED_NAME = re.compile(r"[0-9a-f]{64}\.[A-Za-z0-9]{1,8}")


def hashed_media(storage_path: str) -> bool:
    """/media 下的地址是不是按内容哈希命名的（images.store_image_file、remote_images.store_bytes：
    ``<来源或 remote>/<前两位>/<sha256>.<扩展名>``）。是的话同一个地址永远是同一张图，可以长期缓存；
    不是这种名字的照旧不缓存。"""
    return bool(_HASHED_NAME.fullmatch(storage_path.rsplit("/", 1)[-1]))


@lru_cache(maxsize=64)
def _digest(path: str, mtime_ns: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def asset_version(name: str) -> str:
    """static 下这个文件当前内容的版本号；不是 static 里的文件返回空字符串。"""
    root = STATIC_DIR.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        return ""
    try:
        stat = path.stat()
    except OSError:
        return ""
    if not path.is_file():
        return ""
    return _digest(str(path), stat.st_mtime_ns, stat.st_size)


def asset_url(name: str) -> str:
    version = asset_version(name)
    return f"/static/{name}?v={version}" if version else f"/static/{name}"

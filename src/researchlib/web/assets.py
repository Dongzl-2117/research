"""/static 下的样式和脚本带内容版本号，浏览器和 Cloudflare 可以一直缓存。

页面统一回 ``Cache-Control: private, no-store``（app.privacy_headers）。以前样式和脚本也这样：
每打开一页都要从 Mac mini 经隧道重下一遍 site.css（85KB）和问库的脚本，样式没到页面就出不来。
模板里写 ``{{ asset('site.css') }}``，得到 ``/static/site.css?v=<内容哈希>``；带着当前版本号来取的，
回一年的 immutable 缓存。改了文件版本号就变，新页面取新地址；版本号对不上的照旧不缓存。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"


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

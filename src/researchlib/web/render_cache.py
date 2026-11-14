"""对照页、只看中文页里纯计算那几步的进程内缓存（审核 T8）。

``article_pairs``（逐段核验、切段、配对）和只看中文页的收副本，长文一次几百毫秒到几秒，纯 Python，
占着 GIL，同时打开的别的页面一起变慢。它们只吃字符串和 JSON 一样的数据，同样的输入一定得同样的结果，
所以按全部输入的哈希缓存结果：原文、译文、回执、识图文字哪一样变了，键就变了，不会拿到旧结果。
代码换了（上线）进程重启，缓存跟着清空。

网页的同步路由在线程池里同时跑，缓存放在带锁的对象里（AGENTS.md「规矩」）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

# 一篇长文的配对结果几百 KB 到 1MB 多；64 篇封顶，最多几十 MB。
MAX_ENTRIES = 64


def _key(name: str, args: tuple, kwargs: dict) -> str:
    payload = json.dumps([name, args, sorted(kwargs.items())], ensure_ascii=False, default=repr, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RenderCache:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max = max_entries
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def call(self, name: str, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """``fn(*args, **kwargs)``，同样的输入第二次直接给上次的结果。结果当只读用，别改它。"""
        key = _key(name, args, kwargs)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                self.hits += 1
                return self._items[key]
        # 算的时候不拿锁：两个请求同时算同一篇，各算一遍，结果一样，后写的覆盖前面的。
        value = fn(*args, **kwargs)
        with self._lock:
            self.misses += 1
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.hits = self.misses = 0


PAGES = RenderCache()

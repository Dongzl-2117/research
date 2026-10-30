"""整篇不进列表的标记：articles.raw_meta 里的 ``hidden``。

现在只有 short_post：正文短于来源 min_chars 的帖子入库时标上。正文留着，直接打开还能看；
首页按天、作者时间线、检索和公司页不收。清掉这个键即恢复。
"""
from __future__ import annotations

HIDDEN = "hidden"
SHORT_POST = "short_post"


def visible_sql(alias: str = "") -> str:
    """隐藏的整篇不进列表。"""
    column = f"{alias}.raw_meta" if alias else "raw_meta"
    return f"coalesce({column}->>'{HIDDEN}', '') = ''"


def is_hidden(raw_meta: dict | None) -> bool:
    return bool(str((raw_meta or {}).get(HIDDEN) or "").strip())

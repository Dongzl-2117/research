"""整篇不进列表的标记：articles.raw_meta 里的 ``hidden``。

现在只有 short_post：正文短于来源 min_chars 的帖子入库时标上。正文留着，直接打开还能看；
首页按天、作者时间线、检索和公司页不收。清掉这个键即恢复。
"""
from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

HIDDEN = "hidden"
SHORT_POST = "short_post"
# app_state 里记下每个来源上次按多少字标的短帖：min_chars:<来源>。
LENGTH_STATE_PREFIX = "min_chars:"

logger = logging.getLogger("researchlib.visibility")


def visible_sql(alias: str = "") -> str:
    """隐藏的整篇不进列表。"""
    column = f"{alias}.raw_meta" if alias else "raw_meta"
    return f"coalesce({column}->>'{HIDDEN}', '') = ''"


def is_hidden(raw_meta: dict | None) -> bool:
    return bool(str((raw_meta or {}).get(HIDDEN) or "").strip())


def refresh_length_floors(conn, sources) -> dict[str, int]:
    """来源的 min_chars 和上次不一样，就按新门槛重标库里这个来源的短帖（hidden=short_post）。

    门槛平时只在导入时生效，改了配置以后已入库的不会自己变。启动时调用。
    别的原因隐藏的（比如整源停用）不动。返回 {来源: 改了几篇}，门槛没变的来源不在里面。调用方负责提交。
    """
    from researchlib.ingest.store import body_chars

    changed: dict[str, int] = {}
    for source in sources:
        key = LENGTH_STATE_PREFIX + source.id
        floor = max(0, int(source.min_chars or 0))
        state = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
        if state is not None and state["value"] == str(floor):
            continue
        flipped = 0
        # 从没设过门槛、现在也没有的来源，库里不会有短帖标记，不用扫。
        if floor > 0 or state is not None:
            ids = conn.execute("SELECT id FROM articles WHERE source_id = %s ORDER BY id", (source.id,)).fetchall()
            for item in ids:
                row = conn.execute("SELECT body_md, raw_meta FROM articles WHERE id = %s", (item["id"],)).fetchone()
                meta = dict(row["raw_meta"] or {})
                hidden = meta.get(HIDDEN)
                short = floor > 0 and body_chars(row["body_md"]) < floor
                if short and not hidden:
                    meta[HIDDEN] = SHORT_POST
                elif not short and hidden == SHORT_POST:
                    meta.pop(HIDDEN)
                else:
                    continue
                conn.execute("UPDATE articles SET raw_meta = %s WHERE id = %s", (Jsonb(meta), item["id"]))
                flipped += 1
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at) VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, str(floor)),
        )
        if flipped:
            logger.warning("%s 的短帖门槛改成 %s 字，重标了 %s 篇", source.id, floor, flipped)
        changed[source.id] = flipped
    return changed

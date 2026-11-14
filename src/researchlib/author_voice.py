"""一篇是不是作者本人发的：知识星球的网友帖、「图片」评论壳。

网友帖正文留在库里，但不算星主的观点和作者统计，网站上也不展示。
"""

from __future__ import annotations

import json
import re

# 知识星球：poster_role=guest 是网友帖，正文留着，但不算星主的观点和作者统计。
POSTER_ROLE = "poster_role"
ROLE_GUEST = "guest"
ROLE_OWNER = "owner"


def raw_meta_of(conn, row) -> dict:
    """文章的 raw_meta。查询没带这一列时再读一次。"""
    keys = row.keys() if hasattr(row, "keys") else ()
    if "raw_meta" in keys:
        meta = row["raw_meta"]
    elif conn is not None and row.get("id"):
        found = conn.execute("SELECT raw_meta FROM articles WHERE id = %s", (row["id"],)).fetchone()
        meta = found["raw_meta"] if found else None
    else:
        meta = None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return {}
    return meta if isinstance(meta, dict) else {}


def not_guest_sql(alias: str = "a") -> str:
    """网友帖不进星主的观点和作者统计。没标发帖人的旧数据仍算星主，避免把整库丢掉。"""
    col = f"{alias}.raw_meta" if alias else "raw_meta"
    return f"coalesce({col}->>'poster_role', '') <> 'guest'"


def is_guest(raw_meta: dict | None) -> bool:
    return str((raw_meta or {}).get(POSTER_ROLE) or "") == ROLE_GUEST


# 星主发的纯「图片」帖，导出标题常被拼成「「图片」 评论（N） - 读者名」。
# poster_role 仍是 owner，但标题像读者帖；网站按读者帖同样隐藏（正文库内保留）。
_COMMENT_SHELL_TITLE = re.compile(r"^「图片」\s*评论（\d+）")


def is_comment_shell_title(title: str | None) -> bool:
    return bool(_COMMENT_SHELL_TITLE.match((title or "").strip()))


def hide_from_site(raw_meta: dict | None, title: str | None = "") -> bool:
    """网站入口不展示：网友帖，或标题是「图片」评论壳。"""
    return is_guest(raw_meta) or is_comment_shell_title(title)


def not_comment_shell_sql(alias: str = "a") -> str:
    col = f"{alias}.title" if alias else "title"
    return f"{col} !~ '^「图片」\\s*评论（'"


def site_visible_sql(alias: str = "a") -> str:
    """首页/检索/按天/API 列表共用：非网友帖，且标题不是图片评论壳。"""
    return f"{not_guest_sql(alias)} AND {not_comment_shell_sql(alias)}"


def same_person(poster: str, star: str) -> bool:
    """昵称是不是这个人：去掉首尾空白后一字不差。

    以前去掉括号里的字和空白再比，读者改名「方伟（读者）」「方伟 Rey」就成了星主（审核 I2）。
    星主自己用过的别的昵称写进来源配置的 poster_names，别靠放宽比较来认。
    """
    left, right = (poster or "").strip(), (star or "").strip()
    return bool(left) and left == right


def assign_poster_role(
    meta: dict, star_name: str, aliases: tuple[str, ...] = (), owner_ids: tuple[str, ...] = ()
) -> dict:
    """就地写上 poster_role。没有发帖人名字和 ID 时不猜，旧帖仍按星主处理。

    owner_ids 是星主的账户 ID（来源配置的 owner_ids）。配了它、这篇又带 poster_id 时只按 ID 认：
    昵称谁都能改，ID 改不了。没有 ID 的旧帖才看昵称：和作者名或 aliases（来源配置的 poster_names，
    比如星球昵称「方伟Rey」）一字不差才算星主。
    """
    name = str(meta.get("poster_name") or "").strip()
    poster_id = str(meta.get("poster_id") or "").strip()
    if owner_ids and poster_id:
        owner = poster_id in {str(item).strip() for item in owner_ids}
    elif name:
        owner = any(same_person(name, star) for star in (star_name, *aliases))
    else:
        meta.pop(POSTER_ROLE, None)
        return meta
    meta[POSTER_ROLE] = ROLE_OWNER if owner else ROLE_GUEST
    return meta


def display_author(star_name: str, raw_meta: dict | None) -> tuple[str, bool]:
    """返回 (显示名, 是否算星主本人)。网友帖显示发帖人，不链到星主作者页。"""
    if is_guest(raw_meta):
        return str((raw_meta or {}).get("poster_name") or "").strip() or "星球读者", False
    return star_name, True

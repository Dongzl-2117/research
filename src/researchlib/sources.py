"""来源白名单。不在配置里或未启用的来源，导入必须拒绝。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SOURCE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KINDS = frozenset({"wechat_mp", "zsxq", "feishu", "markdown"})


class SourceRejected(Exception):
    """整次导入被白名单拒绝。退出码 2。"""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    author: str
    kind: str
    enabled: bool
    description: str = ""
    # True：首页按天和 /days。False：只进作者、公司、检索、问库（飞书文库）。
    timeline: bool = True
    # >0：正文（不含评论、空白）不足这么多字的帖子入库时标 hidden=short_post。
    min_chars: int = 0
    # True：整源先不要，入库时标 hidden=source_paused（库里数据保留）。
    paused: bool = False


def load_sources(path: Path) -> list[Source]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("sources")
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path} 里没有 sources 列表")
    sources: list[Source] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{path} 的 sources 项必须是映射")
        source_id = str(item.get("id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        name = str(item.get("name", "")).strip()
        author = str(item.get("author", "")).strip()
        if not SOURCE_ID.match(source_id):
            raise ValueError(f"非法来源 id: {source_id!r}")
        if source_id in seen:
            raise ValueError(f"重复的来源 id: {source_id}")
        if kind not in KINDS:
            raise ValueError(f"{source_id} 的 kind 必须是 {sorted(KINDS)} 之一")
        if not name or not author:
            raise ValueError(f"{source_id} 缺少 name 或 author")
        seen.add(source_id)
        sources.append(
            Source(
                id=source_id,
                name=name,
                author=author,
                kind=kind,
                enabled=bool(item.get("enabled", False)),
                description=str(item.get("description") or ""),
                timeline=_timeline_flag(item, kind, source_id),
                min_chars=int(item.get("min_chars") or 0),
                paused=bool(item.get("paused", False)),
            )
        )
    return sources


def _timeline_flag(item: dict, kind: str, source_id: str) -> bool:
    """没写 timeline 时，飞书默认不进按天，其余来源默认进。"""
    if "timeline" not in item:
        return kind != "feishu"
    value = item.get("timeline")
    if not isinstance(value, bool):
        raise ValueError(f"{source_id} 的 timeline 必须是 true 或 false")
    return value


def require_source(sources: list[Source], source_id: str, *, kind: str | None = None) -> Source:
    by_id = {source.id: source for source in sources}
    source = by_id.get(source_id)
    if source is None:
        known = "、".join(item.id for item in sources)
        raise SourceRejected(f"{source_id} 不在白名单。已配置：{known}")
    if not source.enabled:
        raise SourceRejected(f"{source.id}（{source.name}）在配置里是停用的，拒绝导入")
    if kind is not None and source.kind != kind:
        raise SourceRejected(
            f"{source.id} 的类型是 {source.kind}，不能用 {kind} 导入器。"
            "换对应的命令，或只改配置里的 kind。"
        )
    return source


def enabled_sources(sources: list[Source]) -> list[Source]:
    return [source for source in sources if source.enabled]


def timeline_sources(sources: list[Source]) -> list[Source]:
    """有发布时间、能进首页按天和日汇总的来源。"""
    return [source for source in sources if source.timeline]


def sync_sources(conn, sources: list[Source]) -> None:
    ids = [source.id for source in sources]
    for source in sources:
        conn.execute(
            """
            INSERT INTO sources (id, name, author, kind, enabled, description)
            VALUES (%(id)s, %(name)s, %(author)s, %(kind)s, %(enabled)s, %(description)s)
            ON CONFLICT (id) DO UPDATE SET
              name = EXCLUDED.name,
              author = EXCLUDED.author,
              kind = EXCLUDED.kind,
              enabled = EXCLUDED.enabled,
              description = EXCLUDED.description
            """,
            {
                "id": source.id,
                "name": source.name,
                "author": source.author,
                "kind": source.kind,
                "enabled": source.enabled,
                "description": source.description,
            },
        )
    conn.execute(
        "UPDATE sources SET enabled = false WHERE NOT (id = ANY(%s))",
        (ids,),
    )

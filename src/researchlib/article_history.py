"""按归档时点读取单篇。旧格式的改稿表只保存不同内容，不能证明某一刻是哪一版。"""

import re
from datetime import date, datetime

from researchlib.author_voice import hide_from_site, site_visible_sql
from researchlib.boilerplate import strip_meta
from researchlib.output_safety import redact_subscription_links
from researchlib.present_text import plain_qa_markdown
from researchlib.textutil import SHANGHAI, split_frontmatter
from researchlib.visibility import is_hidden, visible_sql

# 原始评论区即使格式破损也不能当主帖。历史模式只展示结构化且时间已经核实的作者回复。
_COMMENT = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?(?:评论里的回复|评论\s*[（(]\s*\d+\s*[)）])[ \t]*$")


def parse_as_of(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    # 网页表单提交上海本地时间；API 客户端也可以显式提供 UTC 偏移。
    try:
        if len(text) < 16 or text[10] != "T" or date.fromisoformat(text[:10]).isoformat() != text[:10]:
            raise ValueError
        instant = datetime.fromisoformat(text)
        return instant if instant.tzinfo else instant.replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise ValueError("时点须为 ISO 日期时间，例如 2026-11-18T09:30+08:00") from exc


def confirm_commits(conn) -> None:
    # 只能观察已经提交的其他事务。用确定可见的上界，避免把长时间未提交的写入提前算进证据。
    conn.execute("""
        WITH pending AS (
            SELECT xid FROM article_evidence_commits
            WHERE confirmed_at IS NULL AND xid IS DISTINCT FROM pg_current_xact_id_if_assigned()
            ORDER BY xid FOR UPDATE SKIP LOCKED
        )
        UPDATE article_evidence_commits c SET confirmed_at = clock_timestamp() FROM pending
        WHERE pending.xid = c.xid
    """)


def snapshot(conn, article_id, as_of, sources):
    """返回可见版本或无证据状态；当前已隐藏的资料对本入口同样不可见。"""
    confirm_commits(conn)
    now = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    if as_of is not None and as_of > now:
        raise ValueError("历史时点不能晚于现在")
    return read_version(conn, article_id, as_of or now, {s.id: s for s in sources})


def read_version(conn, article_id, cutoff, by_id):
    """纯只读投影。只用查询时点已获确认的历史版本，当前可见性用于撤销访问。"""
    row = conn.execute(
        f"""
        SELECT a.created_at AS first_seen_at, found.*
        FROM articles a
        LEFT JOIN LATERAL (
            SELECT v.*, CASE WHEN v.xid IS NULL THEN v.written_at ELSE c.confirmed_at END AS confirmed_at
            FROM (
                SELECT a.source_id, a.title, a.published_at, a.body_md, a.raw_meta,
                       a.evidence_written_at AS written_at, a.evidence_xid AS xid
                UNION ALL
                SELECT source_id, title, published_at, body_md, raw_meta, written_at, written_xid
                FROM article_evidence_history WHERE article_id = a.id AND replaced_at > %(at)s
            ) v LEFT JOIN article_evidence_commits c ON c.xid = v.xid
            WHERE v.written_at <= %(at)s AND (v.xid IS NULL OR c.confirmed_at <= %(at)s)
            ORDER BY v.written_at DESC LIMIT 1
        ) found ON true
        WHERE a.id = %(id)s AND a.source_id = ANY(%(ids)s) AND {site_visible_sql("a")} AND {visible_sql("a")}
        """,
        {"id": article_id, "at": cutoff, "ids": list(by_id)},
    ).fetchone()
    if row is None:
        return None
    payload = {"id": article_id, "as_of": cutoff.isoformat(), "first_seen_at": row["first_seen_at"].isoformat()}
    if row["written_at"] is None:
        return {**payload, "status": "not_yet_archived" if cutoff < row["first_seen_at"] else "unverified"}
    if row["source_id"] not in by_id or hide_from_site(row["raw_meta"], row["title"]) or is_hidden(row["raw_meta"]):
        return {**payload, "status": "unavailable"}
    if row["published_at"] is not None and row["published_at"] > cutoff:
        return {**payload, "status": "unavailable"}
    source = by_id[row["source_id"]]
    _, md = split_frontmatter(row["body_md"])
    replies = []
    if source.kind == "zsxq":
        md = _COMMENT.split(md, maxsplit=1)[0]
        for reply in row["raw_meta"].get("author_replies") or []:
            if not isinstance(reply, dict) or not isinstance(reply.get("text"), str):
                continue
            try:
                when = datetime.fromisoformat(reply["published_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if when.tzinfo is not None and when <= cutoff:
                replies.append({"published_at": when.isoformat(), "text": redact_subscription_links(reply["text"])})
    return {
        **payload,
        "status": "verified",
        "title": redact_subscription_links(row["title"]),
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
        "version_written_at": row["written_at"].isoformat(),
        "confirmed_at": row["confirmed_at"].isoformat(),
        "body_markdown": redact_subscription_links(strip_meta(plain_qa_markdown(md))).strip(),
        "source": {"id": source.id, "name": source.name},
        "author_replies": replies,
    }

"""改稿和删稿：重新导入时正文变了怎么办。

库里这份常常是唯一的一份：作者删了原文，或者导出工具抓到的已经是删除提示，重导一次就会把
完整的全文换掉。所以：

- 新正文像删除提示（「该内容已被发布者删除」这类），或者比库里短一半以上：不替换，只把这一版
  记成 held（拦下），库里留完整原文。同一版以后再导入也不再记。
- 其余的改动照常替换，替换前把旧版记成 previous。只改了发布时间的不记。

文章页署名行标出「改过」「后来被删」「后来被截短」，点进去看各版之间改了什么。
"""

from __future__ import annotations

import difflib
import re

from psycopg.types.json import Jsonb

from researchlib.boilerplate import strip_meta
from researchlib.eligibility import project_article
from researchlib.textutil import split_frontmatter

# 平台的删除、屏蔽提示。新正文很短又含这些字样，就当作原文被删了。
DELETED_MARKERS = (
    "该内容已被发布者删除",
    "此内容因违规无法查看",
    "此内容被多人投诉",
    "该内容已被删除",
    "此内容已被删除",
    "该文章已被删除",
    "此文章已被删除",
    "内容已删除",
    "已被作者删除",
    "作者已删除",
    "该主题已被删除",
    "该话题已被删除",
    "该帖子已被删除",
    "此帖子已被删除",
    "该内容暂时无法查看",
    "此内容无法查看",
    "内容涉嫌违规",
    "此账号已被屏蔽",
    "此帐号已被屏蔽",
    "账号已注销",
    "帐号已注销",
)
DELETED_MAX_CHARS = 300
SHRINK_MIN_OLD = 400
SHRINK_RATIO = 0.5
REASONS = {
    "deleted": "后来被删", "shrunk": "后来被截短",
    "projection_guard": "回复投影冲突待核", "projection_concurrent": "并发来稿待核",
}
_SPACE = re.compile(r"\s+")


def _plain(text: str) -> str:
    return _SPACE.sub("", text or "")


def split_comments(body_md: str) -> tuple[str, str]:
    """(作者写的正文, 导入时接在后面的「评论里的回复」)。没有那一节时第二项是空串。

    评论会越积越多，重新导入时只有这一节变了，不是作者改了文章：不记旧版，也不拿它判断删稿、截短。
    """
    from researchlib.eligibility import _in_fence
    from researchlib.ingest.parse import COMMENTS_MARKER

    text = body_md or ""
    positions = []
    for marker in (COMMENTS_MARKER, "\n\n## 星主回复（账户已核实）\n\n"):
        start = 0
        while (found := text.find(marker, start)) >= 0:
            if not _in_fence(text, found):
                positions.append(found)
                break
            start = found + len(marker)
    if not positions:
        return text, ""
    index = min(positions)
    return text[:index], text[index:]


def judge_replacement(old_text: str, new_text: str) -> str:
    """新正文能不能替换旧的。返回 "" 表示可以，"deleted" / "shrunk" 表示拦下。"""
    old_plain = _plain(old_text)
    new_plain = _plain(new_text)
    if len(new_plain) <= DELETED_MAX_CHARS and any(marker in new_plain for marker in DELETED_MARKERS):
        if new_plain != old_plain:
            return "deleted"
    if len(old_plain) >= SHRINK_MIN_OLD and len(new_plain) < SHRINK_RATIO * len(old_plain):
        return "shrunk"
    return ""


def held_before(conn, article_id: int, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM article_revisions WHERE article_id = %s AND content_hash = %s AND kind = 'held'",
        (article_id, content_hash),
    ).fetchone()
    return row is not None


def save_revision(
    conn,
    article_id: int,
    *,
    kind: str,
    content_hash: str,
    title: str,
    published_at,
    body_md: str,
    body_text: str,
    raw_meta: dict | None,
    reason: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO article_revisions (
          article_id, kind, reason, content_hash, title, published_at, body_md, body_text, raw_meta
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (article_id, content_hash) DO NOTHING
        """,
        (article_id, kind, reason, content_hash, title, published_at, body_md, body_text, Jsonb(raw_meta or {})),
    )


def article_text(body_md: str) -> str:
    """比两版是不是改了稿时只看作者写的正文：去掉 frontmatter、开头的归档头和评论回复，空白不算。

    星球归档头里的点赞、阅读数每次抓都可能变，标题也会因为补标题的规则变了而重算，这些都不是作者改稿。
    """
    article, _comments = split_comments(body_md or "")
    _front, body = split_frontmatter(article)
    return _plain(strip_meta(body))


def _real_edits(previous: list[dict], current_md: str) -> list[dict]:
    """previous 从旧到新。和替换它的那一版正文一样的旧版不算：以前点赞、阅读数变了也记了一版。"""
    texts = [article_text(row["body_md"]) for row in previous] + [article_text(current_md)]
    return [row for index, row in enumerate(previous) if texts[index] != texts[index + 1]]


def summary(conn, article_ids: list[int]) -> dict[int, dict]:
    """每篇的改动概况：{article_id: {"previous": 次数, "held": 原因}}，没有改动的不在里面。"""
    if not article_ids:
        return {}
    grouped: dict[int, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT article_id, kind, reason, body_md
        FROM article_revisions
        WHERE article_id = ANY(%s)
        ORDER BY article_id, created_at, id
        """,
        (article_ids,),
    ).fetchall():
        grouped.setdefault(row["article_id"], []).append(row)
    if not grouped:
        return {}
    current = {
        row["id"]: row["body_md"] or ""
        for row in conn.execute("SELECT id, body_md FROM articles WHERE id = ANY(%s)", (sorted(grouped),)).fetchall()
    }
    found: dict[int, dict] = {}
    for article_id, rows in grouped.items():
        previous = [row for row in rows if row["kind"] == "previous"]
        held = [row["reason"] for row in rows if row["kind"] != "previous"]
        edits = len(_real_edits(previous, current.get(article_id, "")))
        if edits or held:
            found[article_id] = {"previous": edits, "held": held[-1] if held else ""}
    return found


def label(info: dict | None) -> str:
    """署名行上的一个词。拦下的优先：删稿比改稿更要紧。"""
    if not info:
        return ""
    if info.get("held"):
        return REASONS.get(info["held"], "后来变了")
    if info.get("previous"):
        return "改过"
    return ""


def versions(conn, article: dict) -> list[dict]:
    """当前版和记下来的各版，新的在前。当前版排第一。"""
    rows = conn.execute(
        """
        SELECT id, kind, reason, content_hash, title, published_at, body_md, body_text, raw_meta, created_at
        FROM article_revisions
        WHERE article_id = %s
        ORDER BY created_at DESC, id DESC
        """,
        (article["id"],),
    ).fetchall()
    # 正文没变的旧版不列：每版要和它后面那版比，留着只会多出几条「略去 N 段没变的」。
    previous = [row for row in reversed(rows) if row["kind"] == "previous"]
    edits = {row["id"] for row in _real_edits(previous, article.get("body_md") or "")}
    rows = [row for row in rows if row["kind"] != "previous" or row["id"] in edits]
    current = {
        "id": None,
        "kind": "current",
        "reason": "",
        "content_hash": article["content_hash"],
        "title": article["title"],
        "published_at": article["published_at"],
        "body_text": article["body_text"],
        "created_at": article.get("updated_at"),
    }
    safe_rows = []
    for row in rows:
        projected = project_article({**row, "source_id": article["source_id"]})
        if projected is not None:
            safe_rows.append(projected)
    current["unverified_history"] = len(rows) - len(safe_rows)
    return [current, *safe_rows]


def _paragraphs(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def diff_blocks(old: str, new: str, context: int = 1) -> list[dict]:
    """按段落比两版：[{"kind": "same" | "del" | "add" | "skip", "text": …}]。

    没变的长段落只留前后各 context 段，中间收成一行「略去 N 段没变的」。
    """
    a = _paragraphs(old)
    b = _paragraphs(new)
    blocks: list[dict] = []
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            same = a[i1:i2]
            if len(same) > 2 * context + 1:
                head = same[:context] if blocks else []
                tail = same[-context:] if i2 < len(a) or j2 < len(b) else []
                for text in head:
                    blocks.append({"kind": "same", "text": text})
                skipped = len(same) - len(head) - len(tail)
                if skipped:
                    blocks.append({"kind": "skip", "text": f"略去 {skipped} 段没变的"})
                for text in tail:
                    blocks.append({"kind": "same", "text": text})
            else:
                blocks.extend({"kind": "same", "text": text} for text in same)
            continue
        blocks.extend({"kind": "del", "text": text} for text in a[i1:i2])
        blocks.extend({"kind": "add", "text": text} for text in b[j1:j2])
    return blocks

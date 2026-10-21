"""回扫已经入库的段永平博客。

不读 PDF，也不跑观点模型。做三件事：

- 洗掉正文里的 ``file:///H.HTM`` 页码垃圾，并重算检索文字和段落块。
- 博友「回复段永平」标成非本人（poster_role=guest），不进作者页和观点。
- 没有具体公司，或公司只出现在列举、致谢、闲聊里的，标 ``off_views``，摘掉公司挂靠。

不删文章，也不写改稿记录。新导入已经按同一套规则跳过这些条目；这条命令只处理库里的旧数据。
段永平那节只剩论坛导航的问答整篇搁下：不进按天、作者列表和检索。
"""

from __future__ import annotations

import re
from collections import Counter

from psycopg.types.json import Jsonb

from researchlib.author_voice import ROLE_GUEST, assign_poster_role
from researchlib.boilerplate import clean_markdown, profile_for
from researchlib.chunks import replace_chunks
from researchlib.empty_answer import (
    EMPTY_ANSWER,
    NO_JUDGMENT,
    SUPPRESSED,
    WEAK_JUDGMENT,
    duan_section,
    suppression_for,
    voice_is_empty,
)
from researchlib.ingest.duanyongping import SOURCE_ID
from researchlib.ingest.duanyongping.attach import refine_mentions
from researchlib.ingest.duanyongping.companies import companies_in, library_names
from researchlib.ingest.duanyongping.extract import is_reply_to_duan
from researchlib.ingest.duanyongping.policy import anchored_names
from researchlib.mentions import CompanyMatcher, load_company_rows, mentions_for_document, replace_mentions
from researchlib.textutil import content_hash, markdown_to_text, scrub_file_page_junk, strip_duplicate_title

_FRONT = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.S)
_DUAN_MARK = re.compile(r"(?m)^(?:#{1,6}\s*)?段(?:永平|总)?\s*[:：]")
_GUEST_NAME = re.compile(r"^([\u4e00-\u9fffA-Za-z0-9_]{1,16})\s*回复\s*[:：]?\s*段")


def scrub_stored_markdown(body_md: str) -> str:
    """只洗 frontmatter 后面的正文，题头原样留下。"""
    text = body_md or ""
    match = _FRONT.match(text)
    if not match:
        return scrub_file_page_junk(text)
    return text[: match.end()] + scrub_file_page_junk(text[match.end() :])


def _voice(body_md: str) -> tuple[str, str]:
    """返回 (段永平的文字, 读者和别人的文字)。没有章节时整篇先当作他的正文。"""
    from researchlib.textutil import split_frontmatter, split_markdown_sections

    _front, body = split_frontmatter(body_md or "")
    sections, structured = split_markdown_sections(body)
    if not structured:
        return markdown_to_text(body), ""
    author: list[str] = []
    other: list[str] = []
    for section in sections:
        heading = ""
        match = re.match(r"\A\s*#{1,6}\s+(\S.*?)\s*(?:\n|$)", section)
        if match:
            heading = match.group(1).strip()
            section = section[match.end() :]
        text = markdown_to_text(section)
        if heading in {"读者问", "其他人"}:
            other.append(text)
        else:
            author.append(text)
    return "\n".join(part for part in author if part), "\n".join(part for part in other if part)


def _guest_name(*parts: str) -> str:
    for part in parts:
        for line in (part or "").splitlines():
            match = _GUEST_NAME.match(line.strip())
            if not match:
                continue
            name = match.group(1)
            if name not in {"段永平", "段总", "回复"}:
                return name
    return "博友"


def _has_duan_voice(body_md: str, author: str) -> bool:
    if "## 段永平" in (body_md or ""):
        return len(re.sub(r"\s+", "", author)) >= 6
    return bool(_DUAN_MARK.search(author or ""))


def decide_stored(title: str, body_md: str, raw_meta: dict | None = None) -> dict:
    """纯函数：这篇存量文章该不该进观点，是不是博友回复。"""
    section = duan_section(body_md)
    if section is not None and voice_is_empty(section):
        return {
            "guest": False,
            "off_views": True,
            "suppressed": True,
            "suppressed_as": EMPTY_ANSWER,
            "poster_name": "",
            "reason": "只有论坛导航，没有段永平的话",
        }
    author, other = _voice(body_md)
    reply = any(is_reply_to_duan(text) for text in (title, author, other) if text)
    if reply and not _has_duan_voice(body_md, author):
        return {
            "guest": True,
            "off_views": True,
            "poster_name": _guest_name(title, other, author),
            "reason": "博友回复，不是段永平本人",
        }
    reason = suppression_for(body_md, raw_meta, source_id=SOURCE_ID, title=title)
    if reason == NO_JUDGMENT:
        return {
            "guest": False,
            "off_views": True,
            "off_timeline": True,
            "suppressed": True,
            "suppressed_as": NO_JUDGMENT,
            "poster_name": "",
            "reason": "没有段永平的判断",
        }
    if reason == WEAK_JUDGMENT:
        return {
            "guest": False,
            "off_views": True,
            "suppressed": True,
            "suppressed_as": WEAK_JUDGMENT,
            "poster_name": "",
            "reason": "判断太短，不够明确",
        }
    if reason == EMPTY_ANSWER:
        return {
            "guest": False,
            "off_views": True,
            "suppressed": True,
            "suppressed_as": EMPTY_ANSWER,
            "poster_name": "",
            "reason": "只有论坛导航，没有段永平的话",
        }
    scope = f"{other}\n{author}" if other else author
    real, side, weak = companies_in(scope)
    if not real:
        real = [name for name in anchored_names(title, author) if name not in set(side)]
    library = [name for name in real if name in library_names()]
    if library:
        return {
            "guest": False,
            "off_views": False,
            "poster_name": "",
            "reason": "谈到" + "、".join(library),
        }
    if real:
        reason = "谈到" + "、".join(real) + "（不在公司表，不进观点）"
    elif weak:
        reason = "不是在论证这家公司"
    elif side:
        reason = "公司只出现在举例里"
    else:
        reason = "没有具体公司"
    return {"guest": False, "off_views": True, "poster_name": "", "reason": reason}


def _meta_for(raw_meta, decision: dict) -> dict:
    meta = dict(raw_meta or {})
    if decision.get("suppressed"):
        meta[SUPPRESSED] = decision.get("suppressed_as") or EMPTY_ANSWER
        meta["off_views"] = True
        if decision.get("off_timeline"):
            meta["off_timeline"] = True
        if meta.get("poster_role") == ROLE_GUEST and str(meta.get("poster_name") or "") in {"", "博友"}:
            meta.pop("poster_role", None)
            meta.pop("poster_name", None)
        return meta
    meta.pop(SUPPRESSED, None)
    if decision["guest"]:
        meta["poster_name"] = decision["poster_name"] or "博友"
        meta["off_views"] = True
        assign_poster_role(meta, "段永平")
        return meta
    if meta.get("poster_role") == ROLE_GUEST and str(meta.get("poster_name") or "") in {"", "博友"}:
        meta.pop("poster_role", None)
        meta.pop("poster_name", None)
    if decision["off_views"]:
        meta["off_views"] = True
    else:
        meta.pop("off_views", None)
    return meta


def _mention_key(rows) -> tuple:
    return tuple(sorted((int(company_id), depth, int(hit)) for company_id, depth, hit in rows))


def reclassify_source(conn, *, dry_run: bool = False) -> dict:
    """回扫 source_id = duanyongping-blog。返回计数，不调用模型。"""
    matcher = CompanyMatcher(load_company_rows(conn))
    profile = None
    rows = conn.execute(
        """
        SELECT id, title, body_md, body_text, raw_meta, published_at, duplicate_of
        FROM articles
        WHERE source_id = %s
        ORDER BY id
        """,
        (SOURCE_ID,),
    ).fetchall()
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for row in rows:
        counts["seen"] += 1
        new_md = scrub_stored_markdown(row["body_md"] or "")
        decision = decide_stored(row["title"] or "", new_md, row["raw_meta"])
        meta = _meta_for(row["raw_meta"], decision)
        if row["duplicate_of"] or decision["guest"] or decision["off_views"]:
            mention_rows: list[tuple[int, str, int]] = []
        else:
            mention_rows = mentions_for_document(matcher, row["title"] or "", new_md)
            mention_rows = refine_mentions(matcher, row["title"] or "", new_md, mention_rows, meta)
            if not mention_rows:
                decision = {**decision, "off_views": True, "reason": "没有具体公司"}
                meta = _meta_for(row["raw_meta"], decision)
        if decision["off_views"] or decision["guest"]:
            counts["off_views"] += 1
            reasons[decision["reason"]] += 1
        if decision.get("suppressed"):
            counts["suppressed"] += 1
        if decision["guest"]:
            counts["guest"] += 1
        scrubbed = new_md != (row["body_md"] or "")
        if scrubbed:
            counts["scrubbed"] += 1
        current = conn.execute(
            """
            SELECT company_id, depth, hit_count
            FROM article_mentions
            WHERE article_id = %s
            ORDER BY company_id
            """,
            (row["id"],),
        ).fetchall()
        current_key = tuple((int(item["company_id"]), item["depth"], int(item["hit_count"])) for item in current)
        meta_changed = meta != dict(row["raw_meta"] or {})
        mentions_changed = _mention_key(mention_rows) != current_key
        if mentions_changed:
            counts["mentions"] += 1
        if not scrubbed and not meta_changed and not mentions_changed:
            counts["unchanged"] += 1
            continue
        counts["updated"] += 1
        if dry_run:
            continue
        if profile is None:
            profile = profile_for(conn, SOURCE_ID)
        text = markdown_to_text(strip_duplicate_title(clean_markdown(new_md, profile), row["title"] or ""))
        digest = content_hash(row["title"] or "", new_md, row["published_at"])
        conn.execute(
            """
            UPDATE articles SET
              body_md = %s,
              body_text = %s,
              content_hash = %s,
              raw_meta = %s,
              updated_at = now()
            WHERE id = %s
            """,
            (new_md, text, digest, Jsonb(meta), row["id"]),
        )
        replace_mentions(conn, row["id"], mention_rows)
        replace_chunks(conn, row["id"], row["title"] or "", new_md)
        kept = [company_id for company_id, _depth, _hit in mention_rows]
        if kept:
            conn.execute(
                "DELETE FROM company_views WHERE article_id = %s AND NOT (company_id = ANY(%s))",
                (row["id"], kept),
            )
        else:
            conn.execute("DELETE FROM company_views WHERE article_id = %s", (row["id"],))
        if counts["updated"] % 200 == 0:
            conn.commit()
    if not dry_run:
        conn.commit()
    return {"counts": dict(counts), "reasons": dict(reasons)}


def format_report(report: dict, *, dry_run: bool) -> str:
    counts = report["counts"]
    reasons = report["reasons"]
    lines = [
        f"来源: {SOURCE_ID}",
        "模式: " + ("只计数，不写库" if dry_run else "写回数据库"),
        f"扫描 {counts.get('seen', 0)} 篇，改 {counts.get('updated', 0)} 篇，未改 {counts.get('unchanged', 0)} 篇",
        f"洗掉页码垃圾 {counts.get('scrubbed', 0)} 篇",
        f"不进观点 {counts.get('off_views', 0)} 篇，其中博友回复 {counts.get('guest', 0)} 篇",
        f"搁下 {counts.get('suppressed', 0)} 篇（不进按天、作者列表和检索）",
        f"重算公司挂靠 {counts.get('mentions', 0)} 篇",
        "",
        "不进观点的原因:",
    ]
    if not reasons:
        lines.append("  （无）")
    for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {reason}  {count}")
    lines.append("")
    lines.append("新导入已经跳过这些条目。这条命令不删文章，也不调用观点模型。")
    return "\n".join(lines)

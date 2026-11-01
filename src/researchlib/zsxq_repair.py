"""知识星球存量回写。规则和入库、阅读页是同一套，不另写一套。

别的星球来源（乘风破浪）回写标题和评论：`--source chengfengpolang`。图片文字的整理规则是照方伟定的
（绿气泡记成方伟），只重洗方伟星球和方伟公众号。

- 标题：空的、「无标题」、以省略号结尾的、公司名/话题名冒号壳，
  以及空壳导读（「没有可读文字」「正文为空」），改成完整标题。
  加 `--ai-titles` 时优先用站内 deepseek-flash 起题（含错字抛光），
  失败再退回确定性首句。写不出来的日期兜底不入库。
- 评论：`## 评论（N）` 只留来源作者（来源配置的 author 和 poster_names）。解析不了的不动，在报告里单列。
- 图片文字（只有方伟星球）：星球和公众号里带留言区、划线、赞赏、「（作者）」的，按当前呈现规则重写旁路。
  粘在一行的「分享21 推荐0 划线人数0」「赞赏￥0」「3条留言V 写留言」也算。不重新识图。

改标题或评论会改 `content_hash`，同时把观点、摘要、译文的哈希对齐，避免夜间任务把这几千篇再抽一遍。
"""

from __future__ import annotations

import argparse
import re
import sys

from researchlib.boilerplate import clean_markdown, image_hashes, profile_for
from researchlib.chunks import make_chunks
from researchlib.digest import enough_text_sql
from researchlib.duplicates import fingerprint, store_fingerprint
from researchlib.followups import KIND_TITLE, clear_followup, queued_ids
from researchlib.ingest.store import _body_text
from researchlib.mentions import CompanyMatcher, load_company_rows, mentions_for_document, replace_mentions
from researchlib.ocr import compose_ocr_text, hash_ocr
from researchlib.retime import _realign
from researchlib.textutil import SHANGHAI, content_hash
from researchlib.visibility import is_hidden
from researchlib.wechat_dialogue import (
    _is_author_badge,
    _is_comment_head,
    _is_soft_footer,
    _is_strong_footer,
    _labeled_guest_header,
    present_ocr_pieces,
    present_stored_ocr,
)
from researchlib.zsxq_comments import (
    comment_list_authors,
    drop_guest_comments_text,
    filter_host_comments_markdown,
    guest_comment_authors,
    source_owners,
)
from researchlib.zsxq_title import (
    FANGWEI_ZSXQ,
    polish_title_typos,
    stored_zsxq_title,
    title_needs_ai,
    title_needs_repair,
)
from researchlib.zsxq_title_ai import generate_ai_title

FANGWEI_MP = "fangwei-mp"
_HOST_CHROME = re.compile(
    r"条留言|写留言|划线人数|划线\d|赞赏\s*[￥¥$]|分享\s*\d|推荐\s*\d|（作者）|\(作者\)"
)


def _chrome_lines(text: str) -> bool:
    """整行是留言区、划线、赞赏、分享或「（作者）」。句子里带「赞赏」两个字不算。"""
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if (
            _is_strong_footer(line)
            or _is_comment_head(line)
            or _is_author_badge(line)
            or _is_soft_footer(line)
            or _labeled_guest_header(line)
        ):
            return True
        if "（作者）" in line or "(作者)" in line:
            return True
    return False


def _day(value) -> str:
    if value is None:
        return ""
    return value.astimezone(SHANGHAI).date().isoformat()


def _chrome_under_host(text: str) -> bool:
    presented = present_stored_ocr(text or "")
    for block in presented.split("\n\n"):
        if block.startswith("方伟：") and _HOST_CHROME.search(block):
            return True
    return False


def _rewrite_chunks(conn, article_id: int, title: str, body_md: str, profile, hashes) -> None:
    cleaned = clean_markdown(body_md, profile, hashes)
    chunks = make_chunks(title, cleaned)
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s AND lang = ''", (article_id,))
    if not chunks:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO article_chunks (article_id, ord, heading, body) VALUES (%s, %s, %s, %s)",
            [(article_id, index, heading, text) for index, (heading, text) in enumerate(chunks)],
        )


def _cleaned_ocr(shot_parts: list[str], ocr_text: str) -> str | None:
    """按现规则重写旁路。没有页脚、或重写后和库里一样，返回 None。"""
    raw_blob = "\n".join(shot_parts)
    if shot_parts and (_chrome_lines(raw_blob) or _chrome_lines(ocr_text)):
        new_ocr = compose_ocr_text(shot_parts)
    elif _chrome_lines(ocr_text):
        new_ocr = present_stored_ocr(ocr_text)
    else:
        return None
    if new_ocr == (ocr_text or ""):
        return None
    return new_ocr


def _image_pieces(conn, source_id: str) -> dict[int, list[str]]:
    rows = conn.execute(
        """
        SELECT i.article_id, t.text
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        JOIN articles a ON a.id = i.article_id
        WHERE a.source_id = %s AND a.duplicate_of IS NULL
        ORDER BY i.article_id, i.id
        """,
        (source_id,),
    ).fetchall()
    grouped: dict[int, list[str]] = {}
    for row in rows:
        grouped.setdefault(int(row["article_id"]), []).append(row["text"] or "")
    return grouped


def _digests(conn, source_id: str) -> dict[int, str]:
    # 和页面一样：几乎没字时写下的「正文只有一句，无法归纳」不拿来起标题。
    rows = conn.execute(
        f"""
        SELECT d.article_id, d.summary
        FROM article_digests d
        JOIN articles a ON a.id = d.article_id AND a.content_hash = d.content_hash
        WHERE a.source_id = %s AND a.duplicate_of IS NULL AND {enough_text_sql("a")}
        """,
        (source_id,),
    ).fetchall()
    return {int(row["article_id"]): row["summary"] or "" for row in rows}


def rewash_source_ocr(conn, source_id: str) -> int:
    """只重写这一来源的 OCR 旁路。标题和评论不动。"""
    pieces = _image_pieces(conn, source_id)
    rows = conn.execute(
        """
        SELECT id, ocr_text
        FROM articles
        WHERE source_id = %s AND duplicate_of IS NULL
        ORDER BY id
        """,
        (source_id,),
    ).fetchall()
    changed = 0
    for row in rows:
        new_ocr = _cleaned_ocr(pieces.get(int(row["id"])) or [], row["ocr_text"] or "")
        if new_ocr is None:
            continue
        conn.execute(
            "UPDATE articles SET ocr_text = %s, ocr_hash = %s, updated_at = now() WHERE id = %s",
            (new_ocr, hash_ocr(new_ocr), row["id"]),
        )
        changed += 1
    return changed


def prefer_queued_rows(rows, queued: list[int]):
    """识图刚入队的文章排在全表扫描前面，避免 limit 把它们截在后面。"""
    if not queued:
        return list(rows)
    by_id = {int(row["id"]): row for row in rows}
    seen: set[int] = set()
    ordered = []
    for article_id in queued:
        row = by_id.get(int(article_id))
        if row is None or int(article_id) in seen:
            continue
        ordered.append(row)
        seen.add(int(article_id))
    ordered.extend(row for row in rows if int(row["id"]) not in seen)
    return ordered


def repair_zsxq(
    conn,
    source_id: str = FANGWEI_ZSXQ,
    *,
    spot_ids: list[int] | None = None,
    ai_titles: bool = False,
    settings=None,
    titles_only: bool = False,
    limit: int = 0,
    article_ids: list[int] | None = None,
    owners: tuple[str, ...] = (),
    spot_owners: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    """回写星球的标题、评论；方伟星球再重洗星球和公众号的 OCR 旁路。不提交事务。

    ai_titles：缺/壳/省略号/空壳导读标题优先走站内 flash 起题；settings 必传。
    titles_only：只改标题（增量抓取后补跑用），评论和 OCR 旁路不动。
    limit：最多改这么多标题（0 不限）；增量抓取后补跑用，避免一次打光存量。
    article_ids：只处理这些篇（识图刚入队的）。不传则全表，入队的排在前面。
    owners：评论区只留这些名字（source_owners）。不给时按方伟。
    spot_owners：抽检时按文章的来源认作者，{来源: owners}。不给时按方伟。
    """
    if source_id == FANGWEI_MP:
        changed = rewash_source_ocr(conn, FANGWEI_MP)
        stats = {
            "source_id": source_id,
            "scanned": 0,
            "titles": 0,
            "titles_visible": 0,
            "titles_unresolved": 0,
            "comments_with_section": 0,
            "comments_dirty": 0,
            "comments_cleaned": 0,
            "comments_unparsed": 0,
            "ocr_rewash": 0,
            "ocr_rewash_mp": changed,
            "rewash": True,
            "samples": [],
            "spots": spot_check(conn, spot_ids or [], spot_owners),
        }
        return stats
    # 图片文字的整理把绿气泡记成方伟，只用在方伟星球上。
    rewash = source_id == FANGWEI_ZSXQ and not titles_only
    profile = profile_for(conn, source_id)
    matcher = CompanyMatcher(load_company_rows(conn))
    pieces = _image_pieces(conn, source_id) if rewash else {}
    summaries = _digests(conn, source_id)
    order = "id DESC" if limit else "id"
    params: list = [source_id]
    id_filter = ""
    if article_ids is not None:
        id_filter = " AND id = ANY(%s)"
        params.append([int(item) for item in article_ids])
    rows = conn.execute(
        f"""
        SELECT id, title, body_md, body_text, published_at, content_hash, raw_meta, ocr_text
        FROM articles
        WHERE source_id = %s AND duplicate_of IS NULL{id_filter}
        ORDER BY {order}
        """,
        tuple(params),
    ).fetchall()
    if article_ids is not None:
        queued = [int(item) for item in article_ids]
    elif ai_titles:
        queued = queued_ids(conn, KIND_TITLE, limit or 50)
    else:
        queued = []
    rows = prefer_queued_rows(rows, queued)
    queued_set = set(queued)
    stats = {
        "source_id": source_id,
        "scanned": len(rows),
        "titles": 0,
        "titles_ai": 0,
        "titles_visible": 0,
        "titles_unresolved": 0,
        "comments_with_section": 0,
        "comments_dirty": 0,
        "comments_cleaned": 0,
        "comments_unparsed": 0,
        "ocr_rewash": 0,
        "ocr_rewash_mp": 0,
        "samples": [],
        "spots": [],
        "ai_titles": ai_titles,
        "titles_only": titles_only,
        "rewash": rewash,
    }
    for row in rows:
        article_id = int(row["id"])
        title = row["title"] or ""
        body_md = row["body_md"] or ""
        ocr_text = row["ocr_text"] or ""
        visible = not is_hidden(row["raw_meta"])
        shot_parts = pieces.get(article_id) or []
        cleaned = _cleaned_ocr(shot_parts, ocr_text) if rewash else None
        new_ocr = ocr_text if cleaned is None else cleaned
        if cleaned is not None:
            stats["ocr_rewash"] += 1

        new_title = title
        if title_needs_repair(title, row["body_text"] or ""):
            summary = summaries.get(article_id, "")
            ocr_shown = present_stored_ocr(new_ocr)
            when = _day(row["published_at"])
            # 读者的评论不拿来起题，flash 也不看：只有图的帖以前拿读者那句评论当了标题（23834）。
            body_for_title = drop_guest_comments_text(row["body_text"] or body_md, owners)
            replacement = None
            used_ai = False
            # 空壳导读（「没有可读文字」）也走 flash。半句截断仍不在 title_needs_ai 里。
            if ai_titles and settings is not None and title_needs_ai(title):
                replacement = generate_ai_title(
                    settings,
                    body=body_for_title,
                    ocr=ocr_shown,
                    digest=summary,
                    when=when,
                    old_title=title,
                )
                used_ai = bool(replacement)
            if not replacement:
                replacement = stored_zsxq_title(
                    title,
                    body_for_title,
                    summary=summary,
                    ocr=ocr_shown,
                    when=when,
                    owners=owners,
                )
            if replacement:
                new_title = polish_title_typos(replacement)
                stats["titles"] += 1
                if used_ai:
                    stats["titles_ai"] += 1
                if visible:
                    stats["titles_visible"] += 1
                if len(stats["samples"]) < 8:
                    stats["samples"].append(
                        {"id": article_id, "old": title, "new": new_title, "ai": used_ai}
                    )
            else:
                stats["titles_unresolved"] += 1

        new_md = body_md
        listed = [] if titles_only else comment_list_authors(body_md)
        if listed is None:
            # 有「## 评论（N）」但对不上「- **名字**（时间）」这种列表：不猜，留在报告里。
            stats["comments_with_section"] += 1
            stats["comments_unparsed"] += 1
        elif listed:
            stats["comments_with_section"] += 1
            guests = guest_comment_authors(body_md, owners) or []
            if guests:
                stats["comments_dirty"] += 1
                filtered = filter_host_comments_markdown(body_md, owners)
                if filtered != body_md:
                    new_md = filtered
                    stats["comments_cleaned"] += 1

        # 入队只说明 ocr_hash 变了。标题已经不是空壳就出队，不改字。
        if article_id in queued_set and not title_needs_ai(new_title):
            clear_followup(conn, article_id, KIND_TITLE)
        if new_title == title and new_md == body_md and cleaned is None:
            continue
        hashes = image_hashes(conn, article_id)
        body_text = _body_text(new_title, new_md, profile, hashes)
        digest = content_hash(new_title, new_md, row["published_at"])
        conn.execute(
            """
            UPDATE articles
            SET title = %s, body_md = %s, body_text = %s, content_hash = %s,
                ocr_text = %s, ocr_hash = %s, updated_at = now()
            WHERE id = %s
            """,
            (new_title, new_md, body_text, digest, new_ocr, hash_ocr(new_ocr), article_id),
        )
        if digest != (row["content_hash"] or ""):
            _realign(conn, article_id, {row["content_hash"] or ""}, digest)
            if body_text != (row["body_text"] or ""):
                store_fingerprint(conn, article_id, digest, fingerprint(body_text))
        if new_title != title or new_md != body_md:
            _rewrite_chunks(conn, article_id, new_title, new_md, profile, hashes)
            replace_mentions(conn, article_id, mentions_for_document(matcher, new_title, new_md))
        if limit and titles_only and stats["titles"] >= limit:
            break
    if rewash:
        stats["ocr_rewash_mp"] = rewash_source_ocr(conn, FANGWEI_MP)
    stats["spots"] = spot_check(conn, spot_ids or [], spot_owners)
    return stats


def spot_check(conn, article_ids: list[int], owners: dict[str, tuple[str, ...]] | None = None) -> list[dict]:
    """抽检给定的文章：标题还截不截断、评论剩谁、图片文字的页脚有没有挂到方伟。星球和公众号的都认。

    owners：{来源: 作者名}，评论按这篇所在来源的作者认。不给时按方伟。
    """
    if not article_ids:
        return []
    rows = conn.execute(
        """
        SELECT id, source_id, title, body_md, ocr_text
        FROM articles
        WHERE id = ANY(%s)
        """,
        (list(article_ids),),
    ).fetchall()
    found = {int(row["id"]): row for row in rows}
    image_rows = conn.execute(
        """
        SELECT i.article_id, t.text
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        WHERE i.article_id = ANY(%s)
        ORDER BY i.article_id, i.id
        """,
        (list(article_ids),),
    ).fetchall()
    pieces: dict[int, list[str]] = {}
    for row in image_rows:
        pieces.setdefault(int(row["article_id"]), []).append(row["text"] or "")
    report = []
    for article_id in article_ids:
        row = found.get(article_id)
        if row is None:
            report.append({"id": article_id, "missing": True})
            continue
        listed = comment_list_authors(row["body_md"] or "")
        if listed is None:
            comment = "评论节对不上列表，未改"
        elif not listed:
            comment = "无评论列表"
        else:
            mine = (owners or {}).get(row["source_id"] or "", ())
            guests = guest_comment_authors(row["body_md"] or "", mine) or []
            comment = "仍有别人的评论：" + "、".join(guests) if guests else "评论只剩作者"
        raw_parts = pieces.get(article_id) or []
        presented = present_ocr_pieces(raw_parts) if raw_parts else present_stored_ocr(row["ocr_text"] or "")
        report.append(
            {
                "id": article_id,
                "missing": False,
                "source_id": row["source_id"] or "",
                "title": row["title"] or "",
                "title_truncated": title_needs_repair(row["title"] or ""),
                "comment": comment,
                "chrome_under_host": _chrome_under_host(presented),
            }
        )
    return report


def format_report(stats: dict) -> str:
    titles_only = bool(stats.get("titles_only"))
    lines = [
        f"{'只回写标题' if titles_only else '星球回写'}（{stats['source_id']}），扫描 {stats['scanned']} 篇",
        f"标题回写: {stats['titles']}（其中可见 {stats['titles_visible']}；flash 起题 {stats.get('titles_ai', 0)}）",
        f"标题仍无法收成一句: {stats['titles_unresolved']}",
    ]
    if not titles_only:
        lines += [
            f"评论节: {stats['comments_with_section']}",
            f"评论含别人的: {stats['comments_dirty']}",
            f"评论已清理: {stats['comments_cleaned']}",
            f"评论节未能按列表解析: {stats['comments_unparsed']}",
        ]
    if stats.get("rewash"):
        lines.append(f"对话重洗: 星球 {stats['ocr_rewash']}，公众号 {stats.get('ocr_rewash_mp', 0)}")
    if stats["samples"]:
        lines.append("标题样例:")
        for item in stats["samples"]:
            mark = " [flash]" if item.get("ai") else ""
            lines.append(f"  {item['id']}  {item['old']}  →  {item['new']}{mark}")
    if stats["spots"]:
        lines.append("抽检:")
    for item in stats["spots"]:
        if item.get("missing"):
            lines.append(f"  {item['id']}  库里没有这篇")
            continue
        chrome = "留言区仍挂在方伟下" if item["chrome_under_host"] else "图片文字没有把页脚挂到方伟"
        title = item["title"] or "（空）"
        flag = " 仍截断" if item["title_truncated"] else ""
        source = item.get("source_id") or ""
        lines.append(f"  {item['id']}  {source}  标题「{title}」{flag}  {item['comment']}  {chrome}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="回写星球的截断标题和别人的评论；方伟星球再重洗星球与公众号里带页脚的图片文字"
    )
    parser.add_argument("--source", default=FANGWEI_ZSXQ)
    parser.add_argument("--dry-run", action="store_true", help="只打印会改什么，不提交")
    parser.add_argument(
        "--titles-only",
        action="store_true",
        help="只回写标题，评论和图片文字不动",
    )
    parser.add_argument(
        "--ai-titles",
        action="store_true",
        help="缺/壳/省略号/切不掉的评论残屑标题优先用站内 deepseek-flash 起题（失败退回确定性规则）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多回写这么多标题（0 不限）；增量抓取后补跑建议 30",
    )
    parser.add_argument(
        "--article",
        action="append",
        type=int,
        default=[],
        help="只回写这些文章 id，可重复（识图入队后用）",
    )
    parser.add_argument(
        "--spot",
        default="",
        help="改完以后抽检这些文章，id 逗号分隔（不给就不抽检）",
    )
    args = parser.parse_args(argv)
    spot_ids = [int(part) for part in re.split(r"[\s,]+", args.spot) if part.strip().isdigit()]
    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.sources import load_sources

    settings = Settings.from_env()
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    if args.source != FANGWEI_MP and getattr(sources.get(args.source), "kind", "") != "zsxq":
        raise SystemExit(f"{args.source} 不是知识星球来源，标题规则只认星球")
    owners = {source_id: source_owners(source) for source_id, source in sources.items()}
    conn = prepare(settings)
    try:
        stats = repair_zsxq(
            conn,
            args.source,
            spot_ids=spot_ids,
            ai_titles=args.ai_titles,
            settings=settings if args.ai_titles else None,
            titles_only=args.titles_only,
            limit=args.limit,
            article_ids=args.article or None,
            owners=owners.get(args.source, ()),
            spot_owners=owners,
        )
        print(format_report(stats))
        if args.dry_run:
            conn.rollback()
            print("干跑，未提交")
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

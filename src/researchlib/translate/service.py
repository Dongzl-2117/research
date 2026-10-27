"""把一篇英文文章译成中文 HTML，写入 article_translations。

流程对齐 AIHOT editorial/translate.ts：渲染原文 → 抽段 → shield → Grok 对齐翻译 → 回填 → 入库。
站内 worker / CLI 都走这里；不在站外译完再贴。
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

from researchlib.chunks import replace_translation_chunks
from researchlib.render import render_body
from researchlib.textutil import content_hash, split_frontmatter, strip_duplicate_title
from researchlib.translate.language import is_chinese
from researchlib.translate.llm import GROK_INPUT_CHARS, chat_json, output_tokens
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.segments import (
    chunk_segments,
    shield,
    top_level_blocks,
    translatable_blocks,
    unshield,
)


@dataclass
class TranslateResult:
    article_id: int
    status: str  # translated | partial | skipped
    segments: int = 0
    reason: str | None = None


def translation_problem(sources: list[str], translations: list[str]) -> str | None:
    if len(translations) != len(sources):
        return "count"
    for src, dst in zip(sources, translations, strict=True):
        if src.strip() and not (dst or "").strip():
            return "empty"
    joined_src = "\n".join(sources)
    joined_dst = "\n".join(translations)
    if len(joined_src) >= 80:
        han = len(re.findall(r"[\u4e00-\u9fff]", joined_dst))
        if han < len(joined_src) * 0.15:
            return "short"
    return None


def _render_original(conn, article: dict, data_dir: Path) -> tuple[str, str]:
    """返回 (html, content_hash)。"""
    images = conn.execute(
        "SELECT original_ref, storage_path FROM article_images WHERE article_id = %s",
        (article["id"],),
    ).fetchall()
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    fmt = (article.get("raw_meta") or {}).get("format")
    _fm, body_md = split_frontmatter(article["body_md"])
    if fmt != "html":
        body_md = strip_duplicate_title(body_md, article["title"])
    html = str(render_body(body_md, mapping, fmt))
    digest = content_hash(article["title"], article["body_md"], article.get("published_at"))
    return html, digest


def _translate_batch(parts: list[str], context: dict, data_dir: Path) -> list[str]:
    def once(user: dict) -> list[str]:
        result = chat_json(
            system=SYSTEM_BODY,
            user=user,
            purpose="translate_body",
            subject="batch",
            temperature=0.3,
            max_tokens=min(output_tokens(), max(8000, len("".join(parts)) * 2 + 800)),
            data_dir=data_dir,
        )
        raw = result.data.get("t")
        if not isinstance(raw, list):
            raise RuntimeError("translation JSON missing t[]")
        return [str(item) for item in raw]

    user = {**context, "segments": parts}
    texts = once(user)
    problem = translation_problem(parts, texts)
    if problem == "count":
        # DeepSeek 偶发少段/多段；再要一次严格对齐，仍不对就交给上层跳过。
        texts = once(
            {
                **user,
                "repair": (
                    f"上一次 t 长度是 {len(texts)}，必须正好 {len(parts)}。"
                    "按 segments 下标一一对应重译，只输出 JSON。"
                ),
            }
        )
        problem = translation_problem(parts, texts)
    if problem:
        raise RuntimeError(f"translation alignment: {problem}")
    return texts


def _translate_title(title: str, data_dir: Path) -> str:
    """标题单独译一次，给检索和列表用。失败不影响正文译文。"""
    title = (title or "").strip()
    if not title or is_chinese(title):
        return ""
    try:
        texts = _translate_batch([escape(title)], {}, data_dir)
    except Exception:
        return ""
    plain = re.sub(r"<[^>]+>", "", texts[0] if texts else "")
    return html_lib.unescape(re.sub(r"\s+", " ", plain)).strip()[:300]


def translate_article(conn, article_id: int, *, data_dir: Path) -> TranslateResult:
    article = conn.execute(
        """
        SELECT id, source_id, title, published_at, body_md, body_text, raw_meta, content_hash
        FROM articles WHERE id = %s
        """,
        (article_id,),
    ).fetchone()
    if article is None:
        return TranslateResult(article_id, "skipped", reason="not found")

    sample = article.get("body_text") or article.get("body_md") or ""
    language = (article.get("raw_meta") or {}).get("language")
    if is_chinese(sample, language=language if isinstance(language, str) else None):
        return TranslateResult(article_id, "skipped", reason="already Chinese")

    html, digest = _render_original(conn, article, data_dir)
    blocks = top_level_blocks(html)
    targets = translatable_blocks(html)
    if not targets:
        return TranslateResult(article_id, "skipped", reason="no translatable text")

    shielded = [shield(block) for _idx, block in targets]
    parts = [item.html for item in shielded]
    joined = "\n\n".join(parts)
    context: dict = {
        "articleTitle": (article["title"] or "")[:500],
    }
    if len(joined) * 2 <= GROK_INPUT_CHARS:
        context["articleContext"] = joined
    headings = re.findall(r"<h[2-5][^>]*>(.*?)</h[2-5]>", html, flags=re.I | re.S)
    if headings:
        context["headings"] = [
            re.sub(r"<[^>]+>", "", h).strip()[:160] for h in headings[:24] if re.sub(r"<[^>]+>", "", h).strip()
        ]

    translations: list[str | None] = [None] * len(parts)
    chunks = chunk_segments(parts, GROK_INPUT_CHARS)
    offset = 0
    for chunk in chunks:
        before = "\n".join(translations[i] for i in range(offset) if translations[i])[-1600:]
        after_start = offset + len(chunk)
        after = "\n".join(parts[after_start : after_start + 3])[:1600]
        batch_ctx = {
            **context,
            "before": before or None,
            "after": after or None,
        }
        try:
            got = _translate_batch(chunk, {k: v for k, v in batch_ctx.items() if v}, data_dir)
        except Exception:
            # 整批失败时再试一次
            got = _translate_batch(chunk, {k: v for k, v in batch_ctx.items() if v}, data_dir)
        for i, text in enumerate(got):
            restored = unshield(text, shielded[offset + i])
            translations[offset + i] = restored if restored is not None else text
        offset += len(chunk)

    # 回填到顶层块副本
    filled = list(blocks)
    done = 0
    for (block_index, _src), zh in zip(targets, translations, strict=True):
        if zh:
            filled[block_index] = zh
            done += 1
    if not done:
        return TranslateResult(article_id, "skipped", reason="no batch translated")

    zh_html = "".join(filled)
    zh_text = re.sub(r"<[^>]+>", " ", zh_html)
    zh_text = re.sub(r"\s+", " ", zh_text).strip()
    complete = done == len(targets)
    title_zh = _translate_title(article["title"] or "", data_dir)
    conn.execute(
        """
        INSERT INTO article_translations (
          article_id, lang, content_hash, title_zh, body_html, body_text, complete, origin, segments, updated_at
        )
        VALUES (%s, 'zh', %s, %s, %s, %s, %s, 'model', %s, now())
        ON CONFLICT (article_id) DO UPDATE SET
          content_hash = EXCLUDED.content_hash,
          title_zh = EXCLUDED.title_zh,
          body_html = EXCLUDED.body_html,
          body_text = EXCLUDED.body_text,
          complete = EXCLUDED.complete,
          origin = EXCLUDED.origin,
          segments = EXCLUDED.segments,
          updated_at = now()
        """,
        (article_id, digest, title_zh, zh_html, zh_text, complete, done),
    )
    # 译文切块进检索：中文问题也能找到英文文章，引用落到对照页那一段。
    replace_translation_chunks(conn, article_id)
    conn.commit()
    return TranslateResult(
        article_id,
        "translated" if complete else "partial",
        segments=done,
    )


def list_pending_english(conn, *, limit: int = 20, source_id: str | None = None) -> list[int]:
    """尚无译文、或原文 hash 已变的英文文章。同一篇的重复只译主版本；隐藏的（短帖、每周汇总）不译。"""
    from researchlib.empty_answer import visible_sql

    params: list[object] = []
    where = [
        "a.duplicate_of IS NULL",
        visible_sql("a"),
        "NOT EXISTS ("
        " SELECT 1 FROM article_translations t"
        " WHERE t.article_id = a.id AND t.content_hash = a.content_hash AND t.complete"
        ")",
    ]
    if source_id:
        where.insert(0, "a.source_id = %s")
        params.append(source_id)
    # 粗筛：正文前 400 字汉字不多于拉丁的一半
    sql = f"""
        SELECT a.id
        FROM articles a
        WHERE {" AND ".join(where)}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out: list[int] = []
    for row in rows:
        article = conn.execute(
            "SELECT body_text, body_md, raw_meta FROM articles WHERE id = %s",
            (row["id"],),
        ).fetchone()
        sample = (article or {}).get("body_text") or (article or {}).get("body_md") or ""
        language = ((article or {}).get("raw_meta") or {}).get("language")
        if is_chinese(sample, language=language if isinstance(language, str) else None):
            continue
        out.append(int(row["id"]))
    return out

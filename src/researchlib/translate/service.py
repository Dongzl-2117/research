"""把一篇英文文章译成中文 HTML，写入 article_translations。

流程对齐 AIHOT editorial/translate.ts：渲染原文 → 抽段 → shield → Grok 对齐翻译 → 回填 → 入库。
站内 worker / CLI 都走这里；不在站外译完再贴。
"""

from __future__ import annotations

import html as html_lib
import logging
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
    coarse_segment_reason,
    segments_of,
    shield,
    splice_segments,
    top_level_blocks,
    translatable_blocks,
    unshield,
)

logger = logging.getLogger("researchlib.translate")

# 一批段太多时 deepseek-flash 容易少返回一段或多返回一段。
# 先按这个上限切开；调用后条数仍不一致，再对半拆开重试。
# 单段比字符上限还长时单独成批，不从块中间切开。
TRANSLATE_BATCH_MAX_SEGMENTS = 12
TRANSLATE_BATCH_MAX_CHARS = 24_000
# 拆批时带给下一批的上文 / 下文，和 translate_article 里的窗口一样长。
_SPLIT_WINDOW = 1600


@dataclass
class TranslateResult:
    article_id: int
    status: str  # translated | partial | skipped
    segments: int = 0
    reason: str | None = None


def translation_problem(sources: list[str], translations: list[str]) -> str | None:
    """条数必须和原文块一一对应；有原文的段不能译成空白。

    中文通常比英文短，汉字数占英文字符的比例不再当失败。译文非空就收下。
    """
    if len(translations) != len(sources):
        return "count"
    for src, dst in zip(sources, translations, strict=True):
        if src.strip() and not (dst or "").strip():
            return "empty"
    return None


def _batch_chunks(parts: list[str]) -> list[list[str]]:
    """译制分批。段数和字符都封顶，避免一次要求模型对齐很长的数组。"""
    return chunk_segments(
        parts,
        TRANSLATE_BATCH_MAX_CHARS,
        max_segments=TRANSLATE_BATCH_MAX_SEGMENTS,
    )


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


def _coerce_translations(raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise RuntimeError("translation JSON missing t[]")
    # JSON null 是空白，交给 empty 拒绝；不要写成字符串 "None"。
    return ["" if item is None else str(item) for item in raw]


def _translate_once(parts: list[str], user: dict, data_dir: Path) -> list[str]:
    result = chat_json(
        system=SYSTEM_BODY,
        user=user,
        purpose="translate_body",
        subject="batch",
        temperature=0.3,
        max_tokens=min(output_tokens(), max(8000, len("".join(parts)) * 2 + 800)),
        data_dir=data_dir,
    )
    return _coerce_translations(result.data.get("t"))


def _translate_split(parts: list[str], context: dict, data_dir: Path) -> list[str]:
    """对半拆开再译。左右拼回去之后仍然和原来的段一一对应。"""
    mid = len(parts) // 2
    left_parts = parts[:mid]
    right_parts = parts[mid:]
    left_ctx = dict(context)
    preview = "\n".join(right_parts[:3])[:_SPLIT_WINDOW]
    if preview:
        left_ctx["after"] = preview
    left = _translate_batch(left_parts, left_ctx, data_dir)
    right_ctx = dict(context)
    previous = str(context.get("before") or "")
    merged = "\n".join(piece for piece in (previous, "\n".join(left)) if piece).strip()
    if merged:
        right_ctx["before"] = merged[-_SPLIT_WINDOW:]
    right = _translate_batch(right_parts, right_ctx, data_dir)
    return left + right


def _translate_batch(parts: list[str], context: dict, data_dir: Path) -> list[str]:
    """译一批段。返回值和 parts 等长；对不齐或有空白段就抛错，不把错位数组交出去。"""
    if not parts:
        return []
    # 上一级的 repair 只针对那一批的条数，不能带进更小的一批。
    context = {key: value for key, value in context.items() if key != "repair"}
    user = {**context, "segments": parts}
    texts = _translate_once(parts, user, data_dir)
    problem = translation_problem(parts, texts)
    if problem == "count":
        # 少段 / 多段先按原批重试一次，要求条数正好。
        texts = _translate_once(
            parts,
            {
                **user,
                "repair": (
                    f"上一次 t 长度是 {len(texts)}，必须正好 {len(parts)}。"
                    "按 segments 下标一一对应重译，只输出 JSON。"
                ),
            },
            data_dir,
        )
        problem = translation_problem(parts, texts)
    if problem == "count" and len(parts) > 1:
        logger.info("译文条数对不齐（segments=%s t=%s），对半拆开重试", len(parts), len(texts))
        return _translate_split(parts, context, data_dir)
    if problem:
        raise RuntimeError(f"translation alignment: {problem}")
    return texts



# 对照页在展示时给顶层块盖 id="p-N"。入库去重发生在这之前，比较时要当没盖过。
_STAMPED_PARA_ID = re.compile(r'\s+id="p-\d+"')


def _dedupe_key(html: str) -> str:
    """压空白，并忽略对照页盖上的段落 id。没有这些 id 时和原来的字符串比较一样。"""
    return re.sub(r"\s+", " ", _STAMPED_PARA_ID.sub("", html or "")).strip()


def _dedupe_consecutive_blocks(html: str) -> str:
    """去掉连续相同的顶层块，以及块内连续相同的 <p>（抽验 22195）。

    写入时只对单个译段调用，避免把原文里相邻的两块并成一块。
    对照页在旧译文已经少了重复段时，对原文和译文各跑一遍，把同段重新对齐。
    比较时忽略展示层盖上的 id="p-N"。
    """
    html = html or ""
    blocks = top_level_blocks(html)
    if len(blocks) >= 2:
        out: list[str] = []
        prev_norm: str | None = None
        for block in blocks:
            norm = _dedupe_key(block)
            if prev_norm is not None and norm and norm == prev_norm:
                continue
            out.append(block)
            prev_norm = norm or prev_norm
        html = "".join(out)
    return _dedupe_consecutive_paragraphs(html)


_P_BLOCK = re.compile(r"<p\b[^>]*>.*?</p>", re.I | re.S)
_EMPTY_QUOTE = re.compile(r"<blockquote>\s*</blockquote>", re.I)


def _dedupe_consecutive_paragraphs(html: str) -> str:
    """按文档序去掉连续相同的 <p>（含同一父节点内、blockquote 紧邻重复）。"""
    if not html or "<p" not in html.lower():
        return html
    matches = list(_P_BLOCK.finditer(html))
    if len(matches) < 2:
        return html
    drop: list[tuple[int, int]] = []
    prev_norm: str | None = None
    for match in matches:
        norm = _dedupe_key(match.group(0))
        # 用去标签后的正文长度：短占位（如「是」「好」）不去；测例「第一段…去重。」约 16 字要去。
        plain = re.sub(r"<[^>]+>", "", norm).strip()
        if prev_norm is not None and norm == prev_norm and len(plain) >= 10:
            drop.append(match.span())
            continue
        prev_norm = norm
    if not drop:
        return html
    out: list[str] = []
    last = 0
    for start, end in drop:
        out.append(html[last:start])
        last = end
    out.append(html[last:])
    cleaned = "".join(out)
    return _EMPTY_QUOTE.sub("", cleaned)


def _assemble_zh_html(slots: list[str]) -> str:
    """按原文顶层块原位拼回译文，顶层块数和原文一致。

    一个译段里模型连吐的相同段落收成一份。不跨段删：引用块和下一段相同正文
    都留着，避免只把中文结构削短。
    """
    return "".join(_dedupe_consecutive_blocks(slot) for slot in slots)


def _translate_title(title: str, data_dir: Path) -> str:
    """标题单独译一次，给检索和列表用。失败返回空串，由 complete 规则判为未完成。"""
    title = (title or "").strip()
    if not title or is_chinese(title):
        return ""
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            texts = _translate_batch([escape(title)], {}, data_dir)
        except Exception as exc:  # noqa: BLE001 — 标题失败要重试一次再放弃
            last_exc = exc
            continue
        plain = re.sub(r"<[^>]+>", "", texts[0] if texts else "")
        got = html_lib.unescape(re.sub(r"\s+", " ", plain)).strip()[:300]
        if got:
            return got
    if last_exc is not None:
        return ""
    return ""


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
    chunks = _batch_chunks(parts)
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

    # 按叶子段原位写回。blockquote / 列表 / 表格的外壳留着，只换里面的那一段。
    replacements: dict[int, str] = {}
    done = 0
    for (segment_index, _src), zh in zip(targets, translations, strict=True):
        if zh:
            replacements[segment_index] = _dedupe_consecutive_blocks(zh)
            done += 1
    if not done:
        return TranslateResult(article_id, "skipped", reason="no batch translated")

    zh_html = splice_segments(html, replacements)
    zh_text = re.sub(r"<[^>]+>", " ", zh_html)
    zh_text = re.sub(r"\s+", " ", zh_text).strip()
    title_zh = _translate_title(article["title"] or "", data_dir)
    en_title = (article["title"] or "").strip()
    needs_title_zh = bool(en_title) and not is_chinese(en_title)
    # 英文标题译空不算 complete（抽验 22195：segments 齐但 title_zh 空仍标 complete）
    complete = done == len(targets) and (bool(title_zh) if needs_title_zh else True)
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


# 一次重检最多扫这么多篇完整译文，避免无来源时把全库正文读进内存。
COARSE_SCAN_CAP = 2000


def _source_leaf_count(article: dict) -> int:
    """原文按现在的叶子规则能切成几段。图片地址不影响段数，映射留空。"""
    fmt = (article.get("raw_meta") or {}).get("format")
    _frontmatter, body = split_frontmatter(article.get("body_md") or "")
    if fmt != "html":
        body = strip_duplicate_title(body, article.get("title") or "")
    return len(segments_of(str(render_body(body, {}, fmt))))


def list_coarse_translations(
    conn, *, limit: int = 20, source_id: str | None = None
) -> list[dict[str, object]]:
    """已完成、但结构还是旧粗分段的译文。按发布时间从新到旧，最多返回 limit 篇。

    先看译文 HTML。段数很少、正文又很长时，再拿原文按现在的规则切一遍：
    原文叶子远多于当时记下的 segments，就该重译。重译写回之后对不上这条，不会反复抽中。
    """
    where = [
        "t.complete",
        "t.content_hash = a.content_hash",
        "a.duplicate_of IS NULL",
        "coalesce(t.body_html, '') <> ''",
        "(coalesce(t.segments, 0) <= 40 OR length(coalesce(t.body_html, '')) >= 20000)",
    ]
    params: list[object] = []
    if source_id:
        where.append("a.source_id = %s")
        params.append(source_id)
    params.append(COARSE_SCAN_CAP)
    rows = conn.execute(
        f"""
        SELECT a.id, a.source_id, a.title, a.body_md, a.body_text, a.raw_meta,
               t.segments, t.body_html
        FROM articles a
        JOIN article_translations t ON t.article_id = a.id
        WHERE {" AND ".join(where)}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s
        """,
        params,
    ).fetchall()
    found: list[dict[str, object]] = []
    cap = max(1, limit)
    for row in rows:
        recorded = row["segments"]
        recorded_n = int(recorded) if recorded is not None else None
        reason = coarse_segment_reason(row["body_html"] or "", recorded_segments=recorded_n)
        body_chars = len(row["body_text"] or "")
        if (
            reason is None
            and recorded_n is not None
            and recorded_n <= 30
            and body_chars >= 8_000
        ):
            leaves = _source_leaf_count(row)
            if leaves >= 12 and recorded_n * 3 < leaves and leaves - recorded_n >= 15:
                reason = "segments-below-source"
        if not reason:
            continue
        found.append({"id": int(row["id"]), "source": row["source_id"], "reason": reason})
        if len(found) >= cap:
            break
    return found


def list_pending_english(conn, *, limit: int = 20, source_id: str | None = None) -> list[int]:
    """尚无译文、或原文 hash 已变的英文文章。同一篇的重复只译主版本。"""
    params: list[object] = []
    where = [
        "a.duplicate_of IS NULL",
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

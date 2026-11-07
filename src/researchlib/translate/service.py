"""把一篇英文文章译成中文 HTML，写入 article_translations。

流程对齐 AIHOT editorial/translate.ts：渲染原文 → 抽段 → shield → 站内模型（默认 DeepSeek）按段翻译 → 回填 → 入库。
research-translate 走这里；不在站外译完再贴。

译制读的正文和文章页显示的是同一份：都先过 ``boilerplate.clean_article``。归档头
（如 Stratechery 的 ``- source: member-rss``、``- rss_link:``）、引流尾巴不清洗就会被当成一段送去译，
页面却把它洗掉了，对照从这一段起整页错一行（22092）。
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.boilerplate import clean_article
from researchlib.chunks import replace_translation_chunks
from researchlib.render import canonical_body_html, render_body
from researchlib.textutil import content_hash, split_frontmatter, strip_duplicate_title
from researchlib.translate.harness import (
    fact_brief,
    has_hard_fact,
    note_reject,
    settle_numbers,
)
from researchlib.translate.language import is_chinese
from researchlib.translate.llm import (
    GROK_INPUT_CHARS,
    LLMTransportError,
    XaiLoginRequiredError,
    chat_json,
    model_id,
    output_tokens,
)
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.provenance import (
    ALIGNMENT_VERSION,
    AlignmentError,
    numbers_agree,
    receipt,
    source_manifest,
    structure_problem,
)
from researchlib.translate.provenance import digest as segment_digest
from researchlib.translate.segments import (
    chunk_segments,
    coarse_segment_reason,
    leaf_count_problem,
    segments_of,
    shield,
    splice_segments,
    top_level_blocks,
    translatable_blocks,
    unshield,
)
from researchlib.visibility import visible_sql

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
    # 数字没能自动核对上、照样写入的段数（回执里的 number_review）。
    review: int = 0


class BatchTexts(list[str]):
    """保留实际返回的模型身份；不把运行回执混入发给模型的文章资料。"""

    def __init__(self, values, models=()):
        super().__init__(values)
        self.models = tuple(models)


# 和质量拒绝、请求失败的冷却是同一套数。改这里时把 list_pending_english 的 SQL 一起改。
REJECT_RETRY_BUDGET = 2
REJECT_RETRY_COOLDOWN = timedelta(hours=12)
FAIL_RETRY_COOLDOWNS = (
    timedelta(minutes=15),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=3),
    timedelta(hours=12),
)


def _sql_interval(delta: timedelta) -> str:
    return f"{int(delta.total_seconds())} seconds"


def retry_blocked(attempts: Sequence[tuple[str, datetime]], *, now: datetime) -> bool:
    """同一原文 hash、同一检查版本下，这次要不要先别再花钱。

    对齐拒绝：12 小时内不重试；满 2 次后，直到原文 hash 或 ALIGNMENT_VERSION 变了才再试。
    请求失败：按次数把间隔从 15 分钟拉到 12 小时，不设永久次数上限，宕机结束后还能回来。
    """
    rejected = [stamp for status, stamp in attempts if status == "rejected"]
    failed = [stamp for status, stamp in attempts if status == "failed"]
    if len(rejected) >= REJECT_RETRY_BUDGET:
        return True
    if rejected and max(rejected) > now - REJECT_RETRY_COOLDOWN:
        return True
    if failed:
        latest = max(failed)
        step = FAIL_RETRY_COOLDOWNS[min(len(failed), len(FAIL_RETRY_COOLDOWNS)) - 1]
        if latest > now - step:
            return True
    return False


def record_translation_failure(conn, article_id: int, error: Exception) -> None:
    """由 CLI 在回滚后记录失败，不改译文或检索块；不保存正文或响应。"""
    rejected = isinstance(error, AlignmentError)
    content_hash = None
    row = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (article_id,))
    if row is not None:
        fetched = row.fetchone()
        if fetched:
            content_hash = fetched["content_hash"]
    conn.execute(
        """
        INSERT INTO translation_attempts
          (article_id, status, reason, content_hash, alignment_version)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            article_id,
            "rejected" if rejected else "failed",
            error.code if rejected else "request-failed",
            content_hash,
            ALIGNMENT_VERSION,
        ),
    )
    conn.commit()


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


def _cleaned_body(conn, article: dict) -> str:
    """译制读的正文：和文章页显示前同一条清洗，库里 body_md 不动。

    ``- source: member-rss``、``- rss_link:`` 这类归档头、引流尾巴、二维码和星球导出脚手架都在这里
    去掉。不清洗就直接渲染的话，页面（洗过）和译文（没洗）从第一处脏段起就对不上，
    叶子数差几段，整页错行。
    """
    return clean_article(
        conn,
        int(article["id"]),
        str(article["source_id"]),
        article.get("body_md") or "",
    )


def _render_original(conn, article: dict, data_dir: Path, *, remote: bool = False) -> tuple[str, str]:
    """返回 (html, content_hash)。html 是清洗后的正文渲染出来的叶子来源。

    下载来的外链图（``remote``）不进译制原文，照没下载时写成占位：它们哪天下下来不一定，
    算进回执的话，下下来以后已有的译文就对不上了。页面显示时再换成图（``remote_media``）。
    ``remote=True`` 只给只读核对用：以前图先下好了才译的旧译文，回执是按带图的那一份签的。
    """
    images = conn.execute(
        "SELECT original_ref, storage_path FROM article_images WHERE article_id = %s"
        + ("" if remote else " AND NOT remote"),
        (article["id"],),
    ).fetchall()
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    fmt = (article.get("raw_meta") or {}).get("format")
    html = canonical_body_html(_cleaned_body(conn, article), article["title"], mapping, fmt)
    # raw hash 保持入库身份；实际翻译输入另由 provenance 核验，不自动重排旧稿付费队列。
    digest = content_hash(article["title"], article["body_md"], article.get("published_at"))
    return html, digest


def _coerce_translations(raw: object, ids: list[str] | None = None, hashes: list[str] | None = None) -> list[str]:
    if not isinstance(raw, list):
        raise RuntimeError("translation JSON missing t[]")
    if ids is not None:
        if len(raw) != len(ids):
            raise AlignmentError("response-count")
        seen = set()
        out = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or not isinstance(item.get("html"), str):
                raise AlignmentError("response-identity-missing")
            identity = item.get("id")
            if not isinstance(identity, str) or identity in seen:
                raise AlignmentError("response-id-duplicate")
            seen.add(identity)
            if identity != ids[index]:
                raise AlignmentError("response-id-order")
            if hashes is None or item.get("source_hash") != hashes[index]:
                raise AlignmentError("response-source-hash")
            out.append(item["html"])
        return out
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
    return BatchTexts(
        _coerce_translations(result.data.get("t"), user.get("segment_ids"), user.get("source_hashes")),
        (result.model,),
    )


def _translate_split(parts: list[str], context: dict, data_dir: Path) -> list[str]:
    """对半拆开再译。左右拼回去之后仍然和原来的段一一对应。"""
    mid = len(parts) // 2
    left_parts = parts[:mid]
    right_parts = parts[mid:]
    left_ctx = dict(context)
    if "segment_ids" in context:
        left_ctx["segment_ids"] = context["segment_ids"][:mid]
        left_ctx["source_hashes"] = context["source_hashes"][:mid]
    preview = "\n".join(right_parts[:3])[:_SPLIT_WINDOW]
    if preview:
        left_ctx["after"] = preview
    left = _translate_batch(left_parts, left_ctx, data_dir)
    right_ctx = dict(context)
    if "segment_ids" in context:
        right_ctx["segment_ids"] = context["segment_ids"][mid:]
        right_ctx["source_hashes"] = context["source_hashes"][mid:]
    previous = str(context.get("before") or "")
    merged = "\n".join(piece for piece in (previous, "\n".join(left)) if piece).strip()
    if merged:
        right_ctx["before"] = merged[-_SPLIT_WINDOW:]
    right = _translate_batch(right_parts, right_ctx, data_dir)
    return BatchTexts(left + right, (*getattr(left, "models", ()), *getattr(right, "models", ())))


def _reply(parts: list[str], user: dict, data_dir: Path) -> tuple[list[str], Exception | None]:
    """调一次模型。条数不对、id 或 source_hash 没有逐项回显时把错误交回去，由上一级重来或拆批。

    #217 加逐段身份之后，条数不对在 ``_coerce_translations`` 里就抛了，原来的重试、拆批一次也没走到，
    模型少回一段整篇就作废。
    """
    try:
        texts = _translate_once(parts, user, data_dir)
    except AlignmentError as exc:
        if exc.code.startswith("response-"):
            return [], exc
        raise
    if len(texts) != len(parts):
        return texts, RuntimeError("translation alignment: count")
    return texts, None


def _repair_note(parts: list[str], texts: list[str], mismatch: Exception) -> str:
    if isinstance(mismatch, AlignmentError):
        return (
            f"上一次 t 没有和 segments 逐项对上（{mismatch.code}）。必须正好 {len(parts)} 项，"
            "按顺序原样带回每段的 id 和 source_hash。按 segments 下标一一对应重译，只输出 JSON。"
        )
    return f"上一次 t 长度是 {len(texts)}，必须正好 {len(parts)}。按 segments 下标一一对应重译，只输出 JSON。"


def _translate_batch(parts: list[str], context: dict, data_dir: Path) -> list[str]:
    """译一批段。返回值和 parts 等长；对不齐或有空白段就抛错，不把错位数组交出去。

    少段、多段或身份没对上：先按原批重试一次；仍不对就对半拆开再译，直到一段一批。
    """
    if not parts:
        return []
    # 上一级的 repair 只针对那一批的条数，不能带进更小的一批。
    context = {key: value for key, value in context.items() if key != "repair"}
    user = {**context, "segments": parts}
    texts, mismatch = _reply(parts, user, data_dir)
    if mismatch:
        texts, mismatch = _reply(parts, {**user, "repair": _repair_note(parts, texts, mismatch)}, data_dir)
    if mismatch and len(parts) > 1:
        logger.info("译文和这一批对不齐（segments=%s：%s），对半拆开重试", len(parts), mismatch)
        return _translate_split(parts, context, data_dir)
    if mismatch:
        raise mismatch
    problem = translation_problem(parts, texts)
    if problem:
        raise RuntimeError(f"translation alignment: {problem}")
    return texts


def _restore_batch(
    texts: list[str], shields: list, sources: list[str], offset: int,
) -> tuple[list[str], list[int]]:
    """把一批译文还原成带链接、图片的 HTML。

    链接、图片、代码、占位符不对就抛错（整批重来）。数字对不上的段只记下标，
    交给独立核验；解析器不单独决定收不收。
    """
    restored: list[str] = []
    flagged: list[int] = []
    for index, (text, item, source) in enumerate(zip(texts, shields, sources, strict=True)):
        value = unshield(text, item)
        if value is None:
            raise AlignmentError("unshield")
        problem = structure_problem(source, value)
        if problem:
            raise AlignmentError(f"slot-{offset + index}-{problem}")
        if not numbers_agree(source, value):
            flagged.append(index)
        restored.append(value)
    return restored, flagged


def _exhausted(error: Exception) -> bool:
    """条数或身份对不上的错误从 _translate_batch 出来时，已经重来过、拆到一段一批了。"""
    if isinstance(error, AlignmentError):
        return error.code.startswith("response-")
    return str(error) == "translation alignment: count"


def _settle_batch(
    parts: list[str], context: dict, shields: list, sources: list[str], offset: int, data_dir: Path,
) -> tuple[list[str], list[int], set[str]]:
    """译一批、还原、核对。结构不对时整批再译一次，仍不对就抛错。

    返回还原后的译文、解析器认为数字没对上的下标（只作线索）和实际用到的模型。
    数字收不收由 ``settle_numbers`` 的独立核验决定，解析器不单独否决。
    """
    error: Exception | None = None
    for _attempt in range(2):
        try:
            got = _translate_batch(parts, context, data_dir)
            restored, flagged = _restore_batch(got, shields, sources, offset)
        except (LLMTransportError, XaiLoginRequiredError):
            # 同一次请求已经按 RETRY_DELAYS 重试过。再开一批会把失败时的花费乘二。
            raise
        except Exception as exc:  # noqa: BLE001 — 内容对不齐时整批再试一次
            if _exhausted(exc):
                # 已经重来并拆到一段一批还对不上，整批再走一遍只会把花费翻倍。
                raise
            error = exc
            continue
        return restored, flagged, set(getattr(got, "models", ()))
    raise error if error is not None else AlignmentError("batch")



# 旧辅助函数的比较键；新写入和展示均不使用这些删段函数。
_STAMPED_PARA_ID = re.compile(r'\s+id="p-\d+"')


def _dedupe_key(html: str) -> str:
    """压空白，并忽略对照页盖上的段落 id。没有这些 id 时和原来的字符串比较一样。"""
    return re.sub(r"\s+", " ", _STAMPED_PARA_ID.sub("", html or "")).strip()


def _dedupe_consecutive_blocks(html: str) -> str:
    """兼容旧离线调用，不用于新译制或对照投影；不允许据此判定对应关系。"""
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
    """兼容旧离线调用；真实写入只用有序叶子和持久回执，不调用此函数。"""
    return "".join(_dedupe_consecutive_blocks(slot) for slot in slots)


def _translate_title(title: str, data_dir: Path) -> str:
    """标题单独译一次，给检索和列表用。模型没译好返回空串，由 complete 规则判为未完成。

    连接错误在 chat_json 里已经按预算重试过。这里再吞掉会记成对齐拒绝，两次之后这篇文章就不再译。
    数字没对上时再要一次；两次都没对上就用第一次的，标题不再因为数字写法让整篇作废。
    """
    title = (title or "").strip()
    if not title or is_chinese(title):
        return ""
    source = escape(title)
    fallback = ""
    for _attempt in range(2):
        try:
            texts = _translate_batch([source], {
                "segment_ids": ["title"], "source_hashes": [segment_digest(source)],
            }, data_dir)
        except (LLMTransportError, XaiLoginRequiredError):
            raise
        except Exception:  # noqa: BLE001 — 标题失败要重试一次再放弃
            continue
        plain = re.sub(r"<[^>]+>", "", texts[0] if texts else "")
        got = html_lib.unescape(re.sub(r"\s+", " ", plain)).strip()[:300]
        if not got or structure_problem(source, got):
            continue
        if numbers_agree(source, got):
            return got
        fallback = fallback or got
    return fallback


def reuse_duplicate_sources(sources: list[str], translations: list[str | None]) -> list[str | None]:
    """兼容辅助函数：只给完全相同源 HTML 的空槽补值，不覆盖模型已经返回的文字。

    新译制写入路径不调用它；空槽必须拒绝，不能靠复用把失败标成完成。
    """
    priors: dict[str, str] = {}
    aligned: list[str | None] = []
    for src, zh in zip(sources, translations, strict=True):
        key = src
        aligned.append(zh or priors.get(key))
        if zh:
            priors.setdefault(key, zh)
    return aligned


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
    manifest = source_manifest(html)
    html = manifest["source_snapshot"]
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
    models: set[str] = set()
    review_slots: set[int] = set()
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
            "segment_ids": [manifest["segments"][targets[offset + i][0]]["id"] for i in range(len(chunk))],
            "source_hashes": [manifest["segments"][targets[offset + i][0]]["source_hash"] for i in range(len(chunk))],
        }
        slots = range(offset, offset + len(chunk))
        # 译一批、还原。链接、图片、代码、占位符不对就整批再译一次，再不行整篇不入库。
        # 数字是否保真由 settle_numbers 独立核验：确认冲突的不入库；模型看不出、
        # 修一次仍不确定的段照样写入，序号记进回执 number_review。
        restored, _flagged, used = _settle_batch(
            chunk,
            {k: v for k, v in batch_ctx.items() if v},
            [shielded[i] for i in slots],
            [targets[i][1] for i in slots],
            offset,
            data_dir,
        )
        for i, text in zip(slots, restored, strict=True):
            source_html = targets[i][1]
            segment = manifest["segments"][targets[i][0]]
            if has_hard_fact(source_html) or has_hard_fact(text):
                text, numbers, number_stages = settle_numbers(
                    source_html, text, data_dir,
                    segment_id=segment["id"], source_hash=segment["source_hash"],
                )
                if numbers is None:
                    review_slots.discard(segment["ordinal"])
                elif numbers == "numbers-uncertain":
                    review_slots.add(segment["ordinal"])
                else:
                    note_reject(
                        data_dir,
                        article_id=article_id,
                        slot=f"slot-{i}-{numbers}",
                        kind=numbers,
                        source=source_html,
                        draft=number_stages[-1]["output"],
                        stages=number_stages,
                    )
                    raise AlignmentError(
                        f"slot-{i}-{numbers}",
                        fact_brief(source_html, number_stages[-1]["output"]),
                    )
            translations[i] = text
        models.update(used)
        offset += len(chunk)

    # 按叶子段原位写回。blockquote / 列表 / 表格的外壳留着，只换里面的那一段。
    # 每个响应只归其自身槽位；重复出现也不能用前一份中文覆盖另一份。
    # 字数或相似度都不授权改写响应。完整回执通过后才允许入库。
    aligned = translations
    replacements: dict[int, str] = {}
    done = 0
    for (segment_index, _src), zh in zip(targets, aligned, strict=True):
        if zh:
            replacements[segment_index] = zh
            done += 1
    if not done:
        return TranslateResult(article_id, "skipped", reason="no batch translated")

    zh_html = splice_segments(html, replacements)
    # 展示会把原文和译文再各切一次。写回若让叶子数变了，按位置并排就会错位；
    # 这里直接拒绝，不把错位的译文入库。旧稿对不齐只能整篇重译，不能靠展示层硬配。
    problem = leaf_count_problem(html, zh_html)
    if problem:
        raise RuntimeError(f"translation alignment: {problem}")
    zh_text = re.sub(r"<[^>]+>", " ", zh_html)
    zh_text = re.sub(r"\s+", " ", zh_text).strip()
    title_zh = _translate_title(article["title"] or "", data_dir)
    en_title = (article["title"] or "").strip()
    needs_title_zh = bool(en_title) and not is_chinese(en_title)
    # 英文标题译空不算 complete（抽验 22195：segments 齐但 title_zh 空仍标 complete）
    complete = done == len(targets) and (bool(title_zh) if needs_title_zh else True)
    if not complete:
        raise AlignmentError("incomplete")
    provenance = receipt(
        manifest, zh_html, model=", ".join(sorted(models)) or model_id(), review=sorted(review_slots),
    )
    # 模型请求期间可有新的导入；锁住文章后重新核对，不能把旧输入的结果盖到新原稿上。
    current = conn.execute(
        "SELECT id, source_id, title, published_at, body_md, body_text, raw_meta, content_hash"
        " FROM articles WHERE id = %s FOR UPDATE", (article_id,),
    ).fetchone()
    if current is None or current["content_hash"] != article["content_hash"]:
        raise AlignmentError("source-changed-during-request")
    current_html, _raw_hash = _render_original(conn, current, data_dir)
    if source_manifest(current_html)["source_fingerprint"] != manifest["source_fingerprint"]:
        raise AlignmentError("canonical-source-changed-during-request")
    conn.execute(
        """
        INSERT INTO article_translations (
          article_id, lang, content_hash, title_zh, body_html, body_text, complete,
          origin, segments, provenance, updated_at
        )
        VALUES (%s, 'zh', %s, %s, %s, %s, %s, 'model', %s, %s, now())
        ON CONFLICT (article_id) DO UPDATE SET
          content_hash = EXCLUDED.content_hash,
          title_zh = EXCLUDED.title_zh,
          body_html = EXCLUDED.body_html,
          body_text = EXCLUDED.body_text,
          complete = EXCLUDED.complete,
          origin = EXCLUDED.origin,
          segments = EXCLUDED.segments,
          provenance = EXCLUDED.provenance,
          updated_at = now()
        """,
        (article_id, digest, title_zh, zh_html, zh_text, complete, done, Jsonb(provenance)),
    )
    # 译文切块进检索：中文问题也能找到英文文章，引用落到对照页那一段。
    replace_translation_chunks(conn, article_id)
    conn.commit()
    review = len(provenance.get("number_review") or [])
    if review:
        logger.info("#%s 有 %s 段数字没能自动核对上，照样写入，回执 number_review 里有序号", article_id, review)
    return TranslateResult(
        article_id,
        "translated" if complete else "partial",
        segments=done,
        review=review,
    )


# 一次重检最多扫这么多篇完整译文，避免无来源时把全库正文读进内存。
COARSE_SCAN_CAP = 2000


def _source_leaf_count(conn, article: dict) -> int:
    """原文按现在的叶子规则能切成几段。图片地址不影响段数，映射留空。

    和译制、文章页一样先清洗：重检看的是译制真正能看到的正文，归档头不算叶子。
    """
    fmt = (article.get("raw_meta") or {}).get("format")
    _frontmatter, body = split_frontmatter(_cleaned_body(conn, article))
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
            leaves = _source_leaf_count(conn, row)
            if leaves >= 12 and recorded_n * 3 < leaves and leaves - recorded_n >= 15:
                reason = "segments-below-source"
        if not reason:
            continue
        found.append({"id": int(row["id"]), "source": row["source_id"], "reason": reason})
        if len(found) >= cap:
            break
    return found


# 汉字以外的字符，和 is_chinese 一样只认 U+4E00 到 U+9FFF。
HAN_OUTSIDE = "[^一-鿿]"


def list_pending_english(conn, *, limit: int = 20, source_id: str | None = None) -> list[int]:
    """尚无译文、或原文 hash 已变的英文文章。同一篇的重复只译主版本；隐藏的（短帖、每周汇总）不译。"""
    params: list[object] = [HAN_OUTSIDE]
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
    # 同一原文、同一检查版本下，对齐拒绝有冷却和次数；请求失败只拉长间隔。
    # 进程重启不会把刚拒绝的文章立刻拿回来。hash 或 ALIGNMENT_VERSION 变了，条件对不上，会再试。
    fail_case = ["CASE", "WHEN fail.n <= 1 THEN %s"]
    for index in range(2, len(FAIL_RETRY_COOLDOWNS)):
        fail_case.append(f"WHEN fail.n = {index} THEN %s")
    fail_case.append("ELSE %s")
    fail_case.append("END")
    where.append(
        """NOT EXISTS (
        SELECT 1 FROM (
          SELECT count(*)::int AS n, max(created_at) AS latest
          FROM translation_attempts
          WHERE article_id = a.id
            AND content_hash = a.content_hash
            AND alignment_version = %s
            AND status = 'rejected'
        ) rej
        WHERE rej.n >= %s OR rej.latest > now() - %s::interval
        )"""
    )
    where.append(
        f"""NOT EXISTS (
        SELECT 1 FROM (
          SELECT count(*)::int AS n, max(created_at) AS latest
          FROM translation_attempts
          WHERE article_id = a.id
            AND content_hash = a.content_hash
            AND alignment_version = %s
            AND status = 'failed'
        ) fail
        WHERE fail.n > 0 AND fail.latest > now() - (
          {" ".join(fail_case)}
        )::interval
        )"""
    )
    params.extend([
        ALIGNMENT_VERSION,
        REJECT_RETRY_BUDGET,
        _sql_interval(REJECT_RETRY_COOLDOWN),
        ALIGNMENT_VERSION,
        *[_sql_interval(item) for item in FAIL_RETRY_COOLDOWNS],
    ])
    # 和 language.is_chinese 同一条规则（标了语言听标的；否则正文前 400 字有汉字、且汉字数的两倍
    # 不少于拉丁字母数就算中文），在 SQL 里先筛再 LIMIT。以前先按时间取最新的 limit 篇再挑英文，
    # 不带 --source 时最新的几乎全是中文，永远取不到英文。改规则时两边一起改，集成测试对拍。
    # 库里几乎全是中文：汉字用「删掉非汉字」数、字母用 regexp_count 数，替换和计数的次数都少；
    # OFFSET 0 让每篇只算一遍，不被展开到 WHERE 里算两遍。两万多篇约一秒。
    sql = f"""
        SELECT a.id
        FROM articles a
        CROSS JOIN LATERAL (
            SELECT length(regexp_replace(head, %s, '', 'g')) AS han,
                   regexp_count(head, '[A-Za-z]') AS latin
            FROM (SELECT left(coalesce(nullif(a.body_text, ''), a.body_md, ''), 400) AS head) h
            OFFSET 0
        ) lang
        WHERE {" AND ".join(where)}
          AND CASE a.raw_meta->>'language'
                WHEN 'zh' THEN false
                WHEN 'en' THEN true
                ELSE NOT (lang.han > 0 AND lang.han * 2 >= lang.latin)
              END
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s
    """
    params.append(limit)
    return [int(row["id"]) for row in conn.execute(sql, params).fetchall()]

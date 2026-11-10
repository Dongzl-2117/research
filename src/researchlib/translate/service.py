"""把一篇英文文章译成中文 HTML，写入 article_translations。

流程对齐 AIHOT editorial/translate.ts：渲染原文 → 抽段 → shield → 站内模型（默认 Grok）按段翻译 → 回填 → 入库。
research-translate 走这里；不在站外译完再贴。

译制读的正文和文章页显示的是同一份：都先过 ``boilerplate.clean_article``。归档头
（如 Stratechery 的 ``- source: member-rss``、``- rss_link:``）、引流尾巴不清洗就会被当成一段送去译，
页面却把它洗掉了，对照从这一段起整页错一行（22092）。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.author_voice import not_comment_shell_sql, not_guest_sql
from researchlib.boilerplate import clean_article
from researchlib.chunks import replace_translation_chunks
from researchlib.render import canonical_body_html, render_body
from researchlib.textutil import content_hash, split_frontmatter, strip_duplicate_title
from researchlib.translate.language import is_chinese
from researchlib.translate.llm import (
    GROK_INPUT_CHARS,
    LLMConfigError,
    LLMHTTPError,
    LLMTransportError,
    XaiLoginRequiredError,
    chat_json,
    model_id,
    output_tokens,
    transient_failure,
)
from researchlib.translate.prompts import SYSTEM_BODY
from researchlib.translate.provenance import (
    ALIGNMENT_VERSION,
    PROMPT_VERSION,
    AlignmentError,
    misplaced_slot,
    numbers_agree,
    numeric_anchors,
    prompt_versions,
    receipt,
    reusable_targets,
    reuse_key,
    shape_problem,
    source_manifest,
    structure_problem,
)
from researchlib.translate.provenance import digest as segment_digest
from researchlib.translate.segments import (
    Shielded,
    chunk_segments,
    coarse_segment_reason,
    iter_segments,
    leaf_count_problem,
    segments_of,
    shield,
    splice_segments,
    stable_leaf,
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
# 带给每一批的上文 / 下文有多长。拆批、单段重译也用这个窗口。
_SPLIT_WINDOW = 1600
# 一段在一批里没过（空白、链接或占位符不对、串段），单独再译几次。还不行就先放英文。
_SLOT_RETRIES = 2


@dataclass
class TranslateResult:
    article_id: int
    status: str  # translated | partial | skipped
    segments: int = 0
    reason: str | None = None
    # 数字没能自动核对上、照样写入的段数（回执里的 number_review）。
    review: int = 0
    # 这次没译出来、先放英文的段数（回执里的 untranslated）。下次只补这几段。
    untranslated: int = 0
    # 原文没变、直接用了已有译文的段数。
    reused: int = 0
    # 请求失败（request-failed）时那一次的报错，命令行照印，不用再翻日志猜是断线、限流还是欠费。
    error: str = ""
    # 被断线、超时、限流、5xx 打断没译完：隔几分钟再跑这篇多半能补上。点名补译时命令行据此自动再补一轮。
    retry_later: bool = False


class BatchTexts(list):
    """一批译文，和送去的段按下标一一对应。

    某段到最后也没和请求对上（条数、身份、回答不能用），那一位是 None，原因在 ``problems`` 里。
    保留实际返回的模型身份；不把运行回执混入发给模型的文章资料。
    """

    def __init__(self, values, models=(), problems=None):
        super().__init__(values)
        self.models = tuple(models)
        self.problems = dict(problems or {})


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


def _record_attempt(conn, article_id: int, status: str, reason: str, content_hash: str | None) -> None:
    """记一次没译完的尝试，供待译队列算冷却和次数。不提交，由调用方提交。"""
    conn.execute(
        """
        INSERT INTO translation_attempts
          (article_id, status, reason, content_hash, alignment_version)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (article_id, status, reason, content_hash, ALIGNMENT_VERSION),
    )


def record_translation_failure(conn, article_id: int, error: Exception) -> None:
    """由 CLI 在回滚后记录失败，不改译文或检索块；不保存正文或响应。"""
    rejected = isinstance(error, AlignmentError)
    content_hash = None
    row = conn.execute("SELECT content_hash FROM articles WHERE id = %s", (article_id,))
    if row is not None:
        fetched = row.fetchone()
        if fetched:
            content_hash = fetched["content_hash"]
    _record_attempt(
        conn, article_id,
        "rejected" if rejected else "failed",
        error.code if rejected else "request-failed",
        content_hash,
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


def _translate_split(parts: list[str], context: dict, data_dir: Path) -> BatchTexts:
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
    merged = "\n".join(piece for piece in (previous, "\n".join(text for text in left if text)) if piece).strip()
    if merged:
        right_ctx["before"] = merged[-_SPLIT_WINDOW:]
    right = _translate_batch(right_parts, right_ctx, data_dir)
    return BatchTexts(
        list(left) + list(right),
        (*left.models, *right.models),
        {**left.problems, **{mid + index: code for index, code in right.problems.items()}},
    )


# 400 是这一次请求本身不行（内容被拒、太长、参数不对），换小一点的批可能就过。
_CONTENT_HTTP_STATUS = frozenset({400, 413, 422})


def _service_failure(error: BaseException) -> bool:
    """这次失败是账号或服务那头的事（断线、限流、欠费、5xx、没配密钥），换哪一段问都一样，这篇先停。"""
    if isinstance(error, (LLMTransportError, XaiLoginRequiredError, LLMConfigError)):
        return True
    return isinstance(error, LLMHTTPError) and error.status not in _CONTENT_HTTP_STATUS


def _problem_code(error: Exception) -> str:
    """回答用不了的原因，写进回执、日志和 translation_attempts。不带原文和模型响应。"""
    if isinstance(error, AlignmentError):
        return error.code
    if isinstance(error, LLMHTTPError):
        return f"http-{error.status}"
    if str(error) == "translation alignment: count":
        return "response-count"
    return "response-unusable"


def _reply(parts: list[str], user: dict, data_dir: Path) -> tuple[list[str], Exception | None]:
    """调一次模型。回答用不了时把错误交回去，由上一级重来或拆批。

    用不了包括：条数不对、id 或 source_hash 没有逐项回显、不是 JSON、没有 t[]、这一批被拒（HTTP 400）。
    断线、限流、欠费这类服务那头的错误照样抛出去，换一批问也一样。
    #217 加逐段身份之后，条数不对在 ``_coerce_translations`` 里就抛了，原来的重试、拆批一次也没走到，
    模型少回一段整篇就作废。
    """
    try:
        texts = _translate_once(parts, user, data_dir)
    except (RuntimeError, ValueError) as exc:
        if _service_failure(exc):
            raise
        return [], exc
    if len(texts) != len(parts):
        return texts, RuntimeError("translation alignment: count")
    return texts, None


def _repair_note(parts: list[str], texts: list[str], mismatch: Exception) -> str:
    if isinstance(mismatch, AlignmentError) and mismatch.code.startswith("response-"):
        return (
            f"上一次 t 没有和 segments 逐项对上（{mismatch.code}）。必须正好 {len(parts)} 项，"
            "按顺序原样带回每段的 id 和 source_hash。按 segments 下标一一对应重译，只输出 JSON。"
        )
    if str(mismatch) == "translation alignment: count":
        return f"上一次 t 长度是 {len(texts)}，必须正好 {len(parts)}。按 segments 下标一一对应重译，只输出 JSON。"
    return (
        f"上一次的回答没法用（{_problem_code(mismatch)}）。"
        f"按 segments 下标一一对应重译，必须正好 {len(parts)} 项，只输出 JSON。"
    )


def _translate_batch(parts: list[str], context: dict, data_dir: Path, *, repair: str | None = None) -> BatchTexts:
    """译一批段。返回值和 parts 等长、按下标一一对应，不把错位数组交出去。

    回答用不了（少段、多段、身份没对上、不是 JSON、这一批被拒）：先按原批重来一次；仍不对就对半拆开再译，
    直到一段一批。一段一批也重来过还不行，只空出这一位（None），同一批别的段照常交回。
    空白的译文原样交回，由 ``_settle_batch`` 单独重译那一段。``repair`` 只放进第一次请求。
    """
    if not parts:
        return BatchTexts([])
    # 上一级的 repair 只针对那一批，不能带进更小的一批。
    context = {key: value for key, value in context.items() if key != "repair"}
    user = {**context, "segments": parts}
    texts, mismatch = _reply(parts, {**user, "repair": repair} if repair else user, data_dir)
    if mismatch:
        texts, mismatch = _reply(parts, {**user, "repair": _repair_note(parts, texts, mismatch)}, data_dir)
    if mismatch and len(parts) > 1:
        logger.info("译文和这一批对不齐（segments=%s：%s），对半拆开重试", len(parts), mismatch)
        return _translate_split(parts, context, data_dir)
    if mismatch:
        logger.info("这一段单独问了两次仍没和请求对上（%s），先空着", _problem_code(mismatch))
        return BatchTexts([None], getattr(texts, "models", ()), {0: _problem_code(mismatch)})
    return BatchTexts(texts, getattr(texts, "models", ()))


_NUMBER_REPAIR = (
    "这几段上一次的译文里，数字、日期、金额或百分数没能和原文逐一对上。重译这几段："
    "原文里的每个数都要写出来，数值和单位不变，不漏写，也不要把别段的数字写进来。"
)

# 一段单独重译时告诉模型上一次哪里不对。只是提示，收不收仍按还原和逐段核对。
_KEEP_TOKENS = "⟦0⟧ 这类占位符要原样放进译文，每个恰好一次。"
_SLOT_NOTES = {
    "empty": "上一次这一段的译文是空的。把这一段完整译出来。",
    "unshield": "上一次这一段的占位符或链接没有原样保留。" + _KEEP_TOKENS
    + "<a id=\"L0\"> 这类链接也要原样保留，只译里面的文字。",
    "placeholder": "上一次这一段的占位符没有原样保留。" + _KEEP_TOKENS,
    "links": "上一次这一段的链接对不上。保留每个 <a id=\"Ln\">…</a>，不增不减，id 不变，只译里面的文字。",
    "media": "上一次这一段的图片占位对不上。" + _KEEP_TOKENS,
    "code": "上一次这一段的代码占位对不上。" + _KEEP_TOKENS,
    "numbers-misplaced": "上一次这一段的译文里写的是别的段的数字。只译这一段，"
    "原文里的数字都要写出来，不要写别段的内容。",
    "shape": "上一次这一段的译文会被拆成好几段（多了换行或段落标签）。译成一段，不要加 <br> 或新的段落标签。",
}


@dataclass(frozen=True)
class _Slots:
    """一篇文章的可译段，下标就是 translatable_blocks 里的第几段。"""

    parts: list[str]  # 送给模型的 HTML（链接、图片、代码已经换成占位）
    shields: list[Shielded]
    sources: list[str]  # 原文叶子 HTML
    ids: list[str]
    hashes: list[str]


@dataclass
class _Progress:
    """这一次译出来的段。键是可译段的下标。"""

    values: dict[int, str] = field(default_factory=dict)
    # 数字没对上、照样收下的段。
    flagged: set[int] = field(default_factory=set)
    # 重试完仍没译出来的段和原因。
    gaps: dict[int, str] = field(default_factory=dict)
    models: set[str] = field(default_factory=set)


def _window(slots: _Slots, progress: _Progress, indices: list[int], base: dict) -> dict:
    """这几段的上文（前面已经译好的中文）和下文（后面三段原文），各截 _SPLIT_WINDOW 字。"""
    first, last = indices[0], indices[-1]
    before = "\n".join(progress.values[index] for index in sorted(progress.values) if index < first)
    after = "\n".join(slots.parts[last + 1 : last + 4])
    context = {key: value for key, value in base.items() if key not in {"before", "after"}}
    if before:
        context["before"] = before[-_SPLIT_WINDOW:]
    if after:
        context["after"] = after[:_SPLIT_WINDOW]
    return context


def _ask(slots: _Slots, indices: list[int], context: dict, data_dir: Path, *, repair: str | None = None) -> BatchTexts:
    user = {key: value for key, value in context.items()
            if value and key not in {"segment_ids", "source_hashes", "repair"}}
    user["segment_ids"] = [slots.ids[index] for index in indices]
    user["source_hashes"] = [slots.hashes[index] for index in indices]
    return _translate_batch([slots.parts[index] for index in indices], user, data_dir, repair=repair)


_LEAF_TAG = re.compile(r"\s*<([a-zA-Z][a-zA-Z0-9]*)")


def _restore_slot(slots: _Slots, index: int, text: str) -> tuple[str | None, str | None]:
    """把一段译文还原成带链接、图片的 HTML 并核对。返回 (HTML, None) 或 (None, 问题)。

    除了链接、图片、代码、占位符，还看这一段写回以后是不是仍然只切出一段：
    译文里多了几个 ``<br>`` 或段落标签，写回再切就多出叶子，整篇对照从这里错开。
    定义列表里的 dt、dd 另有打包规则，单看一段判断不了，交给 ``_sign`` 整篇核。
    """
    if not (text or "").strip():
        return None, "empty"
    value = unshield(text, slots.shields[index])
    if value is None:
        return None, "unshield"
    problem = structure_problem(slots.sources[index], value)
    if problem:
        return None, problem
    tag = _LEAF_TAG.match(slots.sources[index])
    name = tag.group(1).lower() if tag else "p"
    if name not in {"dt", "dd"} and len(iter_segments(stable_leaf(value, tag=name, keep_br=True))) != 1:
        return None, "shape"
    return value, None


def _accept(slots: _Slots, indices: list[int], texts: BatchTexts) -> tuple[dict[int, str], set[int], dict[int, str]]:
    """一次回答里逐段还原、核对。返回 (能用的段, 其中数字没对上的段, 不能用的段和原因)。

    一段不能用不连累别的段。身份没对上的段（None）原因带 ``response-`` 或 ``http-`` 前缀，
    它已经单独问过两次，不再单独重译。
    """
    good: dict[int, str] = {}
    flagged: set[int] = set()
    bad: dict[int, str] = {}
    for position, index in enumerate(indices):
        text = texts[position] if position < len(texts) else None
        if text is None:
            bad[index] = getattr(texts, "problems", {}).get(position, "response-missing")
            continue
        value, problem = _restore_slot(slots, index, text)
        if problem:
            bad[index] = problem
            continue
        good[index] = value
        if not numbers_agree(slots.sources[index], value):
            flagged.add(index)
    return good, flagged, bad


def _misplaced(slots: _Slots, neighbors: list[int], good: dict[int, str], flagged: set[int]) -> int | None:
    """数字没对上的段里，有没有哪段写的正好是同一批另一段原文的数字（串段）。返回那一段的下标。"""
    if not flagged:
        return None
    sources = [slots.sources[index] for index in neighbors]
    targets = [good.get(index, "") for index in neighbors]
    positions = [neighbors.index(index) for index in sorted(flagged) if index in neighbors]
    wrong = misplaced_slot(sources, targets, positions)
    return None if wrong is None else neighbors[wrong]


def _identity_gap(problem: str) -> bool:
    return problem.startswith(("response-", "http-"))


def _retry_slot(
    slots: _Slots, index: int, problem: str, base: dict, data_dir: Path, progress: _Progress, neighbors: list[int],
) -> None:
    """这一段单独再译，最多 _SLOT_RETRIES 次，提示带上一次哪里不对。还不行就空着（先放英文），记下原因。"""
    for _attempt in range(_SLOT_RETRIES):
        texts = _ask(slots, [index], _window(slots, progress, [index], base), data_dir,
                     repair=_SLOT_NOTES.get(problem))
        progress.models.update(getattr(texts, "models", ()))
        good, flagged, bad = _accept(slots, [index], texts)
        if index in good:
            if _misplaced(slots, neighbors, good, flagged) == index:
                problem = "numbers-misplaced"
                continue
            progress.values[index] = good[index]
            if index in flagged:
                progress.flagged.add(index)
            return
        problem = bad[index]
        if _identity_gap(problem):
            break
    logger.info("第 %s 段单独重译后仍没过（%s），先放英文", index, problem)
    progress.gaps[index] = problem


def _settle_part(
    slots: _Slots, indices: list[int], base: dict, data_dir: Path, progress: _Progress, neighbors: list[int],
) -> None:
    """问一次、逐段收下能用的；不能用的段单独重译。串段时这次回答整批不信，拆成两半重问。"""
    texts = _ask(slots, indices, _window(slots, progress, indices, base), data_dir)
    progress.models.update(getattr(texts, "models", ()))
    good, flagged, bad = _accept(slots, indices, texts)
    wrong = _misplaced(slots, neighbors, good, flagged)
    if wrong is not None and len(indices) > 1:
        # 数字串到别的段，说明这次回答里段和段可能整体错开了，数字没毛病的段也不能信。
        logger.info("第 %s 段写的是同一批别的段的数字，这 %s 段拆成两半重译", wrong, len(indices))
        mid = len(indices) // 2
        _settle_part(slots, indices[:mid], base, data_dir, progress, neighbors)
        _settle_part(slots, indices[mid:], base, data_dir, progress, neighbors)
        return
    if wrong is not None:
        del good[wrong]
        flagged.discard(wrong)
        bad[wrong] = "numbers-misplaced"
    progress.values.update(good)
    progress.flagged |= flagged
    for index, problem in bad.items():
        if _identity_gap(problem):
            progress.gaps[index] = problem
        else:
            _retry_slot(slots, index, problem, base, data_dir, progress, neighbors)


def _repair_numbers(slots: _Slots, flagged: list[int], base: dict, data_dir: Path, progress: _Progress) -> None:
    """数字对不上的几段合成一次再译。

    重译过了数字核对才换上；还对不上、结构坏了或者请求失败，都留着原来那份，仍记为数字待核。
    这一步只是补救，不会让整篇失败。
    """
    hints = []
    for number, index in enumerate(flagged, 1):
        anchors = numeric_anchors(slots.sources[index])
        if anchors:
            hints.append(f"第{number}段原文的数字：{'、'.join(anchors)}")
    subset = [slots.parts[index] for index in flagged]
    user = {key: value for key, value in _window(slots, progress, flagged, base).items()
            if value and key not in {"segment_ids", "source_hashes", "repair"}}
    user["segment_ids"] = [slots.ids[index] for index in flagged]
    user["source_hashes"] = [slots.hashes[index] for index in flagged]
    user["segments"] = subset
    user["repair"] = _NUMBER_REPAIR + ("；".join(hints) + "。" if hints else "")
    try:
        texts = _translate_once(subset, user, data_dir)
    except Exception as exc:  # noqa: BLE001 — 补救失败就用原来那份，不让整篇作废
        logger.info("数字没对上的 %s 段重译没成（%s），保留原译文", len(flagged), exc)
        return
    if translation_problem(subset, texts):
        return
    fixed = False
    for index, text in zip(flagged, texts, strict=True):
        value = unshield(text, slots.shields[index])
        if (value is not None and structure_problem(slots.sources[index], value) is None
                and numbers_agree(slots.sources[index], value)):
            progress.values[index] = value
            progress.flagged.discard(index)
            fixed = True
    if fixed:
        progress.models.update(getattr(texts, "models", ()))


def _settle_batch(slots: _Slots, indices: list[int], base: dict, data_dir: Path, progress: _Progress) -> None:
    """译一批、还原、逐段核对，结果写进 progress。

    一段不能用（空白、链接或占位符不对、串段）只重译这一段；身份一直对不上的段已经拆到一段一批问过。
    还不行的段记进 progress.gaps，整篇照常往下译。数字没对上的段合成一次补译，仍不行照样收下、记为待核。
    断线、限流这类服务那头的错误照样抛出去，已经收下的段留在 progress 里。
    """
    _settle_part(slots, indices, base, data_dir, progress, list(indices))
    flagged = sorted(index for index in progress.flagged if index in set(indices))
    if flagged:
        _repair_numbers(slots, flagged, base, data_dir, progress)



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
    """标题单独译一次，给检索和列表用。模型没译好返回空串，这篇记为没译完，下次只补标题。

    连接错误在 chat_json 里已经按预算重试过，这里照样抛出去，由 translate_article 先把正文存下。
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
        except Exception as exc:  # noqa: BLE001 — 标题失败要重试一次再放弃
            if _service_failure(exc):
                raise
            continue
        text = texts[0] if texts else None
        if not text:
            continue
        plain = re.sub(r"<[^>]+>", "", text)
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


def _pending_batches(slots: _Slots, done: dict[int, str]) -> list[list[int]]:
    """还没有译文的段按原文顺序分批。连续的几段放一起，再按段数和字数封顶。

    第一次译整篇时就是全文按 _batch_chunks 切；只补几段时，隔开的段各自成批，上下文另带。
    """
    runs: list[list[int]] = []
    for index in range(len(slots.parts)):
        if index in done:
            continue
        if runs and runs[-1][-1] == index - 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    batches: list[list[int]] = []
    for run in runs:
        start = 0
        for chunk in _batch_chunks([slots.parts[index] for index in run]):
            batches.append(run[start : start + len(chunk)])
            start += len(chunk)
    return batches


def _previous_translation(conn, article_id: int) -> dict | None:
    row = conn.execute(
        "SELECT content_hash, title_zh, body_html, complete, provenance"
        " FROM article_translations WHERE article_id = %s AND lang = 'zh'",
        (article_id,),
    )
    found = row.fetchone() if row is not None else None
    return found if isinstance(found, dict) else None


def _reuse(previous: dict | None, sources: list[str]) -> dict[int, str]:
    """已有回执里原文没变的段，直接拿来用。同一段原文出现几次，按先后一一对上，多出来的照常译。"""
    known = reusable_targets((previous or {}).get("provenance"))
    if not known:
        return {}
    reused: dict[int, str] = {}
    for index, source in enumerate(sources):
        options = known.get(reuse_key(source))
        if options:
            reused[index] = options.pop(0)
    return reused


def _breaking_slots(manifest: dict, html: str, kept: dict[int, str]) -> list[int]:
    """哪几段一写回，叶子数或外壳就和原文不一样了。对半找，只在整篇写回对不上时才走。"""

    def broken(keys: list[int]) -> bool:
        candidate = splice_segments(html, {key: kept[key] for key in keys})
        return bool(leaf_count_problem(html, candidate) or shape_problem(manifest, candidate))

    def find(keys: list[int]) -> list[int]:
        if not keys or not broken(keys):
            return []
        if len(keys) == 1:
            return keys
        mid = len(keys) // 2
        return find(keys[:mid]) + find(keys[mid:])

    return find(sorted(kept))


def _sign(
    manifest: dict, html: str, replacements: dict[int, str], gaps: set[int], *, model: str,
    prompts: list[int] | None = None,
) -> tuple[str, dict, set[int]]:
    """写回并签回执。返回 (译文 HTML, 回执, 写回后才露出问题、退回英文的叶子序号)。

    每段收下前已经单独核过链接、图片、代码和占位符。这里兜住只有整篇写回以后才看得出的问题
    （译文被再切成两段、外壳变了），把出问题的那几段退回英文，其余照签。找不出是哪几段就整篇不入库。
    """
    kept = dict(replacements)
    dropped: set[int] = set()
    while True:
        zh_html = splice_segments(html, kept)
        try:
            if leaf_count_problem(html, zh_html):
                raise AlignmentError("receipt-count")
            proof = receipt(manifest, zh_html, model=model, untranslated=sorted(gaps | dropped), prompts=prompts or ())
            return zh_html, proof, dropped
        except AlignmentError as exc:
            slot = re.fullmatch(r"slot-(\d+)-[\w-]+", exc.code)
            if slot and int(slot.group(1)) in kept:
                bad = [int(slot.group(1))]
            elif exc.code in {"receipt-count", "receipt-skeleton"}:
                bad = _breaking_slots(manifest, html, kept)
            else:
                raise
            if not bad:
                raise
            logger.info("写回后叶子或外壳对不上（%s），第 %s 个叶子退回英文", exc.code, bad)
            for ordinal in bad:
                kept.pop(ordinal, None)
            dropped.update(bad)
            if not kept:
                raise


def translate_article(conn, article_id: int, *, data_dir: Path, reuse: bool = True) -> TranslateResult:
    """译一篇写进 article_translations。

    一段出错不连累整篇：身份、链接、占位符一直对不上或者串段的那几段先放英文，序号记进回执的
    ``untranslated``，这篇记为没译完（complete 为假），过了冷却再进队列，下次只补这几段。
    请求断线、限流时已经译好的段先存下。``reuse`` 为真时，已有回执里原文没变的段直接用，不再花钱重译；
    想整篇重来（换了模型，或者觉得译得不好）时传 False。上一份回执不是现在这一版提示词译的，
    也按 False 走：整篇按现在的提示词重译，哪段重译失败才退回旧译文，免得一篇里新旧两种写法混着。
    """
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
    entries = [manifest["segments"][index] for index, _block in targets]
    slots = _Slots(
        parts=[item.html for item in shielded],
        shields=shielded,
        sources=[block for _index, block in targets],
        ids=[entry["id"] for entry in entries],
        hashes=[entry["source_hash"] for entry in entries],
    )
    joined = "\n\n".join(slots.parts)
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

    previous = _previous_translation(conn, article_id)
    earlier_prompts = prompt_versions((previous or {}).get("provenance"))
    # 已有回执里原文没变的段。默认直接用，只译剩下的；--fresh 或者上一份是旧提示词译的，整篇重译，
    # 哪段重译失败才退回用它，重译不会让已有的好译文变差。
    known = _reuse(previous, slots.sources)
    reuse = reuse and earlier_prompts == [PROMPT_VERSION]
    progress = _Progress(values=dict(known) if reuse else {})
    service_error: Exception | None = None
    batches = _pending_batches(slots, progress.values)
    for number, indices in enumerate(batches):
        try:
            # 一段数字没对上：同一批补译一次，还不行照样写入，回执里记进 number_review。
            # 一段链接、图片、代码、占位符不对，或者数字串到同一批别的段：只重译这一段，还不行先放英文。
            _settle_batch(slots, indices, context, data_dir, progress)
        except (RuntimeError, ValueError) as exc:
            if not _service_failure(exc) or isinstance(exc, XaiLoginRequiredError):
                raise
            # 断线、限流、欠费：后面的批问了也一样。已经译好的段先存下，下次接着译。
            service_error = exc
            logger.info("#%s 第 %s/%s 批请求失败（%s），已译的段先存下", article_id, number + 1, len(batches), exc)
            break

    translated_now = set(progress.values) - set(known if reuse else ())
    if not reuse:
        for index, value in known.items():
            progress.values.setdefault(index, value)
    reused = len(set(progress.values) - translated_now)

    title_zh = ""
    en_title = (article["title"] or "").strip()
    needs_title_zh = bool(en_title) and not is_chinese(en_title)
    old_title = str((previous or {}).get("title_zh") or "") if (previous or {}).get("content_hash") == digest else ""
    if needs_title_zh:
        if reuse and old_title:
            title_zh = old_title
        elif service_error is None:
            try:
                title_zh = _translate_title(article["title"] or "", data_dir)
            except (RuntimeError, ValueError) as exc:
                if not _service_failure(exc) or isinstance(exc, XaiLoginRequiredError):
                    raise
                service_error = exc
        title_zh = title_zh or old_title

    gaps = {index: progress.gaps.get(index, "request-failed" if service_error else "not-asked")
            for index in range(len(slots.parts)) if index not in progress.values}
    # 一段都没译出来；或者整篇重译（--fresh）一段新的都没译成、全靠退回旧译文：不写库，把错交给 CLI 记冷却。
    if not progress.values or (not reuse and not translated_now and (progress.gaps or service_error)):
        if service_error is not None:
            raise service_error
        failures = progress.gaps or gaps
        first = min(failures)
        raise AlignmentError(f"slot-{first}-{failures[first]}")

    # 按叶子段原位写回。blockquote / 列表 / 表格的外壳留着，只换里面的那一段。没译出来的段留着英文。
    # 每个响应只归其自身槽位；重复出现也不能用前一份中文覆盖另一份。
    # 字数或相似度都不授权改写响应。完整回执通过后才允许入库。
    ordinal_of = [index for index, _block in targets]
    replacements = {ordinal_of[index]: value for index, value in progress.values.items()}
    # 复用的段是上一次那个模型译的，回执里两次的模型都记上。
    models = set(progress.models)
    if reused:
        earlier = str(((previous or {}).get("provenance") or {}).get("model") or "")
        models.update(name.strip() for name in earlier.split(",") if name.strip())
    # 回执记下这篇里的段用过哪几版提示词：这次新译的是现在这一版，复用或退回的旧段沿用上一份回执的。
    prompts = {PROMPT_VERSION} if translated_now else set()
    if reused:
        prompts.update(earlier_prompts)
    zh_html, provenance, dropped = _sign(
        manifest, html, replacements, {ordinal_of[index] for index in gaps},
        model=", ".join(sorted(models)) or model_id(), prompts=sorted(prompts),
    )
    for index, ordinal in enumerate(ordinal_of):
        if ordinal in dropped:
            gaps[index] = "receipt-shape"
    done = len(slots.parts) - len(gaps)
    zh_text = re.sub(r"<[^>]+>", " ", zh_html)
    zh_text = re.sub(r"\s+", " ", zh_text).strip()
    # 英文标题译空不算 complete（抽验 22195：segments 齐但 title_zh 空仍标 complete）
    complete = not gaps and (bool(title_zh) if needs_title_zh else True)
    # 和库里那份一字不差（都是复用的，或者重译出来一样）就不再写，检索块也不重切。
    # 回执也要一样：清洗规则改过以后原文指纹变了，译文一个字没变也得重签，不然对照页一直按过期回执分开展示。
    unchanged = bool(previous) and (
        previous.get("content_hash") == digest
        and previous.get("body_html") == zh_html
        and str(previous.get("title_zh") or "") == title_zh
        and bool(previous.get("complete")) == complete
        and previous.get("provenance") == json.loads(json.dumps(provenance))
    )
    if not unchanged:
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
    # 哪几类问题让段先放英文（links、shape、http-400…），只有标题没译出来时是 title-empty。
    # 打印出来、记进 translation_attempts.reason，以后按原因查，不必再翻日志。
    why = ",".join(sorted(set(gaps.values()))) if gaps else "title-empty"
    if not complete:
        # 没译完的也记一次尝试：断线按请求失败拉长间隔，别的按拒绝算（12 小时冷却、同一版本 2 次）。
        # 不记的话待译队列马上又把它拿回来。
        _record_attempt(
            conn, article_id,
            "failed" if service_error else "rejected",
            "request-failed" if service_error else f"untranslated:{why}",
            article["content_hash"],
        )
    conn.commit()
    review = len(provenance.get("number_review") or [])
    if review:
        logger.info("#%s 有 %s 段数字没能自动核对上，照样写入，回执 number_review 里有序号", article_id, review)
    if gaps:
        logger.info("#%s 有 %s 段没译出来，先放英文，回执 untranslated 里有序号：%s",
                    article_id, len(gaps), sorted(set(gaps.values())))
    return TranslateResult(
        article_id,
        "translated" if complete else "partial",
        segments=done,
        reason=("request-failed" if service_error else why) if not complete else ("unchanged" if unchanged else None),
        review=review,
        untranslated=len(gaps),
        reused=reused,
        error=str(service_error)[:300] if service_error is not None and not complete else "",
        retry_later=not complete and transient_failure(service_error),
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
# 网站显示的：不是网友帖，标题也不是「图片」评论壳（和首页、检索、API 的 site_visible_sql 一样，只是标题为空的照算）。
# 网站不显示的不译：和摘要、观点、识图一样，译了也没人看得到。
SITE_VISIBLE = f"({not_guest_sql('a')} AND coalesce({not_comment_shell_sql('a')}, true))"


def list_pending_english(conn, *, limit: int = 20, source_id: str | None = None) -> list[int]:
    """尚无译文、或原文 hash 已变、或没译完的英文文章。同一篇的重复只译主版本；隐藏的（短帖、每周汇总）不译。

    网站不显示的网友帖、「图片」评论壳也不译：和摘要、观点、识图一样，译了也没人看得到。
    已经译完（``complete`` 且 ``content_hash`` 对得上）的不进队列。换 LLM 提供方不会把这些篇重译；
    没译完的下次接着补，复用回执里没变的段。
    """
    params: list[object] = [HAN_OUTSIDE]
    where = [
        "a.duplicate_of IS NULL",
        visible_sql("a"),
        SITE_VISIBLE,
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

"""对照页质量检查：硬指标 + 可选 LLM 抽验。

只读。页面的对照由 ``pair.bilingual_pairs`` 现算，这里读同一份：原文先过和文章页同一条
清洗、md 链接改写和图片文字，译文过同一遍白名单。指标只判断，不改库、不重译。

五条硬指标，和人工抽验的口径一致：

1. 巨型 pair：一对英文的可见文字达到 2000 字。按句切开之后不该再有。
2. 近重复组：页面上还留着重复、整句包含或近前缀的长英文。展示层只在两边叶子一样多时
   按位置收；条数对不齐的旧译文会留下来。
3. long-empty-ZH：对上的那一对英文够长（80 字）却没有中文。
4. 归档头：正文里还留着 ``member-rss`` / ``rss_link`` / ``- source:`` 这类行。
5. 叶子/对数异常：展示硬排的两边叶子差；差到 40 段按失败算，差一两段只告警。

LLM 抽验用站内默认模型（DeepSeek deepseek-flash，thinking + reasoning_effort=max），
只对抽到的段落打分挑刺，结果回给调用方，不写库。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from researchlib.boilerplate import clean_article
from researchlib.config import Settings
from researchlib.mdlinks import finish_html, load_md_index, prepare_body_html
from researchlib.present_text import polish_display_text
from researchlib.render import inline_image_ocr, sanitize_html
from researchlib.sources import load_sources
from researchlib.translate.llm import extract_json
from researchlib.translate.pair import (
    BilingualPair,
    bilingual_pairs,
    display_blocks,
    redundant_pairs,
    visible_text,
)
from researchlib.translate.segments import iter_segments
from researchlib.zsxq_comments import source_owners
from researchlib.zsxq_title import shorten_long_title

# 一叶英文可见文字到这么多就算巨型。历史失败都在 2000 字上下。
GIANT_EN_CHARS = 2_000
# 够长的一段没中文才算漏译；短行、纯标点可以缺。
LONG_EN_CHARS = 80
# 展示硬排的两边叶子差到这么多段，按失败算；差一两段只告警。
LEAF_GAP_FAIL = 40

# 归档头会漏进译文的几种写法。``来源：`` 单独出现可能是正文里正常的说法，不单独认。
_ARCHIVE_PATTERNS = (
    re.compile(r"member-rss", re.I),
    re.compile(r"\brss_link\b", re.I),
    re.compile(r"(?m)^\s*[-*•]\s*(?:source|sn|topic_id|原链接|原文链接|原始数据)\s*[:：]", re.I),
)

_OCR_SQL = """
    SELECT i.content_hash, t.text
    FROM article_images i
    JOIN image_texts t ON t.content_hash = i.content_hash
    WHERE i.article_id = %s AND btrim(t.text) <> ''
"""

LLM_MAX_PAIRS = 12
LLM_PAIR_CHARS = 1500

LLM_SYSTEM = """你是中英对照页的排版质检员。用户给你一篇英文文章和它的中文译文，按位置并排的抽样段落（en / zh），
以及这一篇现算出来的硬指标（巨型段落、近重复、长英文没中文、归档头、两边叶子差）。

en / zh 可能因为长度被截断：截断的项带 "truncated": true，末尾是「…」。只比较给出的部分，
不要把截断当成漏译、错位或段落边界问题。只判断段落是否成对，不评价译文文笔，不改写译文：
- misaligned：这一对的英文和中文讲的不是同一段（错位、张冠李戴）。
- missing-zh：英文是一整段而中文为空。
- duplicate：这一段英文在前面已经出现过，或者只是一段更长英文的一部分。
- other：其他明显的排版问题。

只依据给出的段落，不要猜没给你的正文。输出 JSON 对象：
{"verdict": "pass" | "warn" | "fail", "score": 0-100,
 "issues": [{"index": 段落下标, "kind": "misaligned|missing-zh|duplicate|other", "note": "一两句中文"}],
 "summary": "一两句中文结论"}
没有问题时 issues 是空数组，verdict 是 pass。"""


class QaError(RuntimeError):
    """质检读不到需要的输入，或模型返回的东西不能当结论。"""


@dataclass(frozen=True)
class ArticleTexts:
    """一篇文章在对照页上真正显示的英文和中文 HTML。"""

    article_id: int
    source_id: str
    title: str
    en_html: str
    zh_html: str


@dataclass(frozen=True)
class CheckIssue:
    code: str
    level: str  # fail | warn
    count: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "level": self.level, "count": self.count, "detail": self.detail}


@dataclass
class BilingualReport:
    article_id: int
    source_id: str
    title: str
    pairs: int
    en_leaves: int
    zh_leaves: int
    en_blocks: int
    zh_blocks: int
    paired: int
    tail_en: int
    tail_zh: int
    max_en_chars: int
    giant: tuple[int, ...]
    near_duplicates: tuple[int, ...]
    long_empty_zh: tuple[int, ...]
    archive_markers: tuple[str, ...]
    issues: tuple[CheckIssue, ...] = ()
    llm: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        if any(issue.level == "fail" for issue in self.issues):
            return "fail"
        if self.issues:
            return "warn"
        return "pass"

    @property
    def fine_gap(self) -> int:
        return abs(self.en_leaves - self.zh_leaves)

    @property
    def display_gap(self) -> int:
        return abs(self.en_blocks - self.zh_blocks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "articleId": self.article_id,
            "source": self.source_id,
            "title": self.title,
            "status": self.status,
            "pairs": self.pairs,
            "enLeaves": self.en_leaves,
            "zhLeaves": self.zh_leaves,
            "displayLeaves": [self.en_blocks, self.zh_blocks],
            "fineGap": self.fine_gap,
            "displayGap": self.display_gap,
            "paired": self.paired,
            "tail": {"en": self.tail_en, "zh": self.tail_zh},
            "maxEnChars": self.max_en_chars,
            "giant": list(self.giant),
            "nearDuplicates": list(self.near_duplicates),
            "longEmptyZh": list(self.long_empty_zh),
            "archiveMarkers": list(self.archive_markers),
            "issues": [issue.as_dict() for issue in self.issues],
            "llm": self.llm,
        }


def _plain(html: str) -> str:
    """去标签的可见文字，压空白。只看有没有，不按标点比较。"""
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def _archive_markers(html: str) -> tuple[str, ...]:
    text = html_lib.unescape(html or "")
    found: list[str] = []
    for pattern in _ARCHIVE_PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0).strip()
            if snippet and snippet not in found:
                found.append(snippet)
    return tuple(found)


def report_from_html(article_id: int, source_id: str, title: str, en_html: str, zh_html: str) -> BilingualReport:
    """在已经渲染好的两边 HTML 上算指标。单测和多篇批量都走这一个入口。"""
    pairs: list[BilingualPair] = bilingual_pairs(en_html, zh_html) or []
    en_leaves = len(iter_segments(en_html))
    zh_leaves = len(iter_segments(zh_html))
    blocks = display_blocks(en_html, zh_html)
    en_blocks = len(blocks[0]) if blocks else 0
    zh_blocks = len(blocks[1]) if blocks else 0
    paired = min(en_blocks, zh_blocks)
    tail_en = max(0, en_blocks - paired)
    tail_zh = max(0, zh_blocks - paired)

    texts = [visible_text(pair.en or "") for pair in pairs]
    giant = tuple(index for index, text in enumerate(texts) if len(text) >= GIANT_EN_CHARS)
    long_empty = tuple(
        index
        for index in range(min(paired, len(pairs)))
        if len(texts[index]) >= LONG_EN_CHARS and not (pairs[index].zh or "").strip()
    )
    near_duplicates = tuple(redundant_pairs(pairs))
    markers = tuple(dict.fromkeys(_archive_markers(en_html) + _archive_markers(zh_html)))

    issues: list[CheckIssue] = []
    if giant:
        issues.append(
            CheckIssue(
                "giant-pair",
                "fail",
                len(giant),
                f"单对英文 >= {GIANT_EN_CHARS} 字，下标 {_preview(giant)}",
            )
        )
    if near_duplicates:
        issues.append(
            CheckIssue(
                "near-duplicate",
                "fail",
                len(near_duplicates),
                f"页面上仍是重复/包含/近前缀，下标 {_preview(near_duplicates)}",
            )
        )
    if long_empty:
        issues.append(
            CheckIssue(
                "long-empty-zh",
                "fail",
                len(long_empty),
                f"英文 >= {LONG_EN_CHARS} 字却没有中文，下标 {_preview(long_empty)}",
            )
        )
    if markers:
        issues.append(CheckIssue("archive-header", "fail", len(markers), "、".join(markers[:3])))
    display_gap = abs(en_blocks - zh_blocks)
    if display_gap >= LEAF_GAP_FAIL:
        issues.append(
            CheckIssue(
                "leaf-gap",
                "fail",
                display_gap,
                f"展示硬排的叶子差 {display_gap} 段（英文 {en_blocks}、中文 {zh_blocks}）",
            )
        )
    elif display_gap:
        issues.append(
            CheckIssue(
                "leaf-gap",
                "warn",
                display_gap,
                f"展示硬排的叶子差 {display_gap} 段（英文 {en_blocks}、中文 {zh_blocks}）",
            )
        )
    return BilingualReport(
        article_id=article_id,
        source_id=source_id,
        title=title,
        pairs=len(pairs),
        en_leaves=en_leaves,
        zh_leaves=zh_leaves,
        en_blocks=en_blocks,
        zh_blocks=zh_blocks,
        paired=paired,
        tail_en=tail_en,
        tail_zh=tail_zh,
        max_en_chars=max((len(text) for text in texts), default=0),
        giant=giant,
        near_duplicates=near_duplicates,
        long_empty_zh=long_empty,
        archive_markers=markers,
        issues=tuple(issues),
    )


def _preview(indexes: Sequence[int], limit: int = 8) -> str:
    shown = ", ".join(str(index) for index in indexes[:limit])
    return shown + (" …" if len(indexes) > limit else "")


def load_article_texts(conn, settings: Settings, article_id: int) -> ArticleTexts | None:
    """按文章页的顺序拼出英文和中文 HTML。没有译文返回 None。"""
    article = conn.execute(
        """
        SELECT id, source_id, title, body_md, body_text, raw_meta
        FROM articles WHERE id = %s
        """,
        (article_id,),
    ).fetchone()
    if article is None:
        raise QaError(f"#{article_id} 不存在")
    translation = conn.execute(
        "SELECT body_html FROM article_translations WHERE article_id = %s",
        (article_id,),
    ).fetchone()
    if translation is None or not translation["body_html"]:
        return None
    zh_blob = translation["body_html"] or ""
    link_index = None
    if ".md" in (article["body_md"] or "").lower() or ".md" in zh_blob.lower():
        link_index = load_md_index(conn, article["source_id"])
    source = next(
        (item for item in load_sources(settings.sources_path) if item.id == article["source_id"]),
        None,
    )
    shown_md = polish_display_text(
        clean_article(conn, article_id, article["source_id"], article["body_md"])
    )
    display_title = shorten_long_title(article["title"] or "", article.get("body_text") or "")
    images = conn.execute(
        "SELECT original_ref, storage_path FROM article_images WHERE article_id = %s",
        (article_id,),
    ).fetchall()
    mapping = {row["original_ref"]: f"/media/{row['storage_path']}" for row in images}
    fmt = (article.get("raw_meta") or {}).get("format")
    parent = str((article.get("raw_meta") or {}).get("parent") or "")
    ocr = {
        row["content_hash"]: row["text"] or ""
        for row in conn.execute(_OCR_SQL, (article_id,)).fetchall()
    }
    en_html = inline_image_ocr(
        prepare_body_html(
            shown_md,
            display_title,
            mapping,
            fmt,
            link_index,
            parent,
            owners=source_owners(source) if source else (),
        ),
        ocr,
    )
    zh_html = inline_image_ocr(finish_html(sanitize_html(zh_blob), link_index, parent), ocr)
    return ArticleTexts(
        article_id=int(article["id"]),
        source_id=str(article["source_id"]),
        title=str(article["title"] or ""),
        en_html=en_html,
        zh_html=zh_html,
    )


def check_article(conn, settings: Settings, article_id: int) -> BilingualReport | None:
    """一篇的硬指标。没有译文时返回 None。"""
    texts = load_article_texts(conn, settings, article_id)
    if texts is None:
        return None
    return report_from_html(texts.article_id, texts.source_id, texts.title, texts.en_html, texts.zh_html)


def translated_article_ids(conn, *, limit: int = 20, source_id: str | None = None) -> list[int]:
    """最近译完的篇：完整、hash 对得上，按译文更新时间从新到旧。"""
    where = [
        "t.complete",
        "t.content_hash = a.content_hash",
        "a.duplicate_of IS NULL",
        "coalesce(t.body_html, '') <> ''",
    ]
    params: list[object] = []
    if source_id:
        where.insert(0, "a.source_id = %s")
        params.append(source_id)
    params.append(max(1, limit))
    rows = conn.execute(
        f"""
        SELECT a.id
        FROM articles a
        JOIN article_translations t ON t.article_id = a.id
        WHERE {" AND ".join(where)}
        ORDER BY t.updated_at DESC NULLS LAST, a.id DESC
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _review_samples(
    report: BilingualReport,
    pairs: Sequence[BilingualPair],
    *,
    max_pairs: int = LLM_MAX_PAIRS,
    chars: int = LLM_PAIR_CHARS,
) -> list[dict[str, Any]]:
    """抽给模型看的段落：先挑指标点名的那几对，再按整篇的头、中、尾补几组。"""
    if not pairs:
        return []
    wanted: list[int] = []
    for index in (*report.giant, *report.near_duplicates, *report.long_empty_zh):
        if 0 <= index < len(pairs) and index not in wanted:
            wanted.append(index)
    total = len(pairs)
    for fraction in (0.0, 1 / 3, 2 / 3, 1.0):
        start = min(max(0, int(total * fraction) - 1), max(0, total - 3))
        for index in range(start, min(total, start + 3)):
            if index not in wanted:
                wanted.append(index)
    wanted = wanted[:max_pairs]
    samples: list[dict[str, Any]] = []
    for index in wanted:
        pair = pairs[index]
        en_text = visible_text(pair.en or "")
        zh_text = _plain(pair.zh or "")
        samples.append(
            {
                "i": index,
                "en": en_text[:chars],
                "zh": zh_text[:chars],
                "truncated": len(en_text) > chars or len(zh_text) > chars,
            }
        )
    return samples


def build_review_messages(
    report: BilingualReport,
    pairs: Sequence[BilingualPair],
    *,
    max_pairs: int = LLM_MAX_PAIRS,
    chars: int = LLM_PAIR_CHARS,
) -> list[dict[str, str]]:
    """LLM 抽验的 messages。段落是抽样，不含整篇原文。"""
    payload = {
        "articleId": report.article_id,
        "source": report.source_id,
        "title": report.title,
        "stats": {
            "pairs": report.pairs,
            "enLeaves": report.en_leaves,
            "zhLeaves": report.zh_leaves,
            "displayGap": report.display_gap,
            "giant": list(report.giant),
            "nearDuplicates": list(report.near_duplicates),
            "longEmptyZh": list(report.long_empty_zh),
            "archiveMarkers": list(report.archive_markers),
        },
        "pairs": _review_samples(report, pairs, max_pairs=max_pairs, chars=chars),
    }
    return [
        {"role": "system", "content": LLM_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_review(raw: str) -> dict[str, Any]:
    """把模型的 JSON 收成固定字段。verdict 不认识、score 不是数字都算失败。"""
    try:
        data = extract_json(raw)
    except ValueError as exc:
        raise QaError(f"LLM 抽验返回的不是 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise QaError("LLM 抽验的 JSON 根节点不是对象")
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "warn", "fail"}:
        raise QaError(f"LLM 抽验的 verdict 不认识：{verdict!r}")
    raw_score = data.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float, str)):
        raise QaError(f"LLM 抽验的 score 不是 0-100 的整数：{raw_score!r}")
    try:
        score = max(0, min(100, int(raw_score)))
    except (TypeError, ValueError):
        raise QaError(f"LLM 抽验的 score 不是 0-100 的整数：{raw_score!r}") from None
    raw_issues = data.get("issues")
    issues: list[dict[str, Any]] = []
    if isinstance(raw_issues, list):
        for item in raw_issues[:20]:
            if not isinstance(item, dict):
                continue
            issues.append(
                {
                    "index": item.get("index"),
                    "kind": str(item.get("kind") or "other")[:40],
                    "note": str(item.get("note") or "")[:300],
                }
            )
    return {
        "verdict": verdict,
        "score": score,
        "issues": issues,
        "summary": str(data.get("summary") or "")[:500],
    }


def review_with_llm(
    settings: Settings,
    report: BilingualReport,
    pairs: Sequence[BilingualPair],
) -> dict[str, Any]:
    """调站内默认模型抽验一篇。只返回结论，不写库。"""
    from researchlib.llm import chat

    messages = build_review_messages(report, pairs)
    text, model = chat(settings, messages)
    parsed = parse_review(text)
    parsed["model"] = model
    return parsed


def pairs_for_review(conn, settings: Settings, article_id: int) -> list[BilingualPair]:
    """重新取这一篇的对照，给 LLM 抽验拼抽样用。"""
    texts = load_article_texts(conn, settings, article_id)
    if texts is None:
        return []
    return bilingual_pairs(texts.en_html, texts.zh_html) or []


__all__ = [
    "GIANT_EN_CHARS",
    "LEAF_GAP_FAIL",
    "LONG_EN_CHARS",
    "BilingualReport",
    "CheckIssue",
    "QaError",
    "build_review_messages",
    "check_article",
    "load_article_texts",
    "pairs_for_review",
    "parse_review",
    "report_from_html",
    "review_with_llm",
    "translated_article_ids",
]

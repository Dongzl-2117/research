"""段内漏句抽查：让模型逐对看译文有没有漏掉原文里的句子、数字、名字。只读，不改库、不重译。

    docker compose exec app research-bilingual-qa --omissions --source stratechery --limit 50

段对按回执来（``provenance.segments`` 里每段的原文和译文），和模型当时译的那一段一一对应，
不受对照页并段、去重的影响。没有回执的旧稿跳过。先放英文的段（``untranslated``）不送。

模型摘出来的「漏掉的原文」要能在这一段英文里逐字找到才算数，找不到的（模型自己编的、改写过的）丢掉，
只计个数。结果只是线索：措辞不同、并句被当成漏的误报要人看一眼。这一步不拦入库，也不改回执。
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from researchlib.translate.llm import LLMConfigError, LLMHTTPError, LLMTransportError, chat_json
from researchlib.translate.provenance import untranslated_slots
from researchlib.translate.xai_oauth import XaiLoginRequiredError

# 一次请求最多几对、英文最多多少字。整篇几十段，十几对一批，请求次数和译制差不多。
MAX_PAIRS = 12
MAX_CHARS = 8000
# 一处漏句最多摘这么多字，报告里不贴整段。
QUOTE_CHARS = 300

SYSTEM = """你是译文核对员。用户给你同一篇文章里的若干对段落：en 是英文原文的一段，zh 是这一段的中文译文。
逐对看中文有没有漏掉英文里的内容：整句或分句没译、数字或日期没写、人名公司名产品名没出现、
否定或限定（not、only、at least、probably 这类）丢了。
措辞不同、语序调整、意译、几句并成一句、省掉 "Well," "So," 这类口头语都不算漏。只比较同一对的 en 和 zh。
漏了的，把漏掉的英文原样摘出来：逐字照抄 en 里的原文，每处不超过 40 个词，不要改写、不要加省略号。拿不准的不报。
输出 JSON：{"t": [{"i": 下标, "missing": ["漏掉的英文原文", ...]}]}。只列有漏的对；都没漏时 t 是空数组。"""

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_LETTER = re.compile(r"[A-Za-z]")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def _text(html: str) -> str:
    return _SPACE.sub(" ", html_lib.unescape(_TAG.sub(" ", html or ""))).strip()


def _norm(text: str) -> str:
    """比较摘句用：统一引号和破折号、压空白、忽略大小写，去掉两头的标点。"""
    value = _SPACE.sub(" ", (text or "").translate(_QUOTES)).strip().casefold()
    return value.strip(" .,;:!?\"'()[]")


@dataclass(frozen=True)
class Pair:
    ordinal: int
    en: str
    zh: str


@dataclass
class Usage:
    """模型用量，按 DeepSeek 返回的 usage 累加。没有就是 0。"""

    prompt: int = 0
    cache_hit: int = 0
    completion: int = 0
    reasoning: int = 0
    requests: int = 0

    def add(self, usage: dict | None) -> None:
        self.requests += 1
        if not isinstance(usage, dict):
            return
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.cache_hit += int(usage.get("prompt_cache_hit_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            self.reasoning += int(details.get("reasoning_tokens") or 0)

    def merge(self, other: Usage) -> None:
        self.prompt += other.prompt
        self.cache_hit += other.cache_hit
        self.completion += other.completion
        self.reasoning += other.reasoning
        self.requests += other.requests

    def as_dict(self) -> dict[str, int]:
        return {"requests": self.requests, "prompt": self.prompt, "cacheHit": self.cache_hit,
                "completion": self.completion, "reasoning": self.reasoning}


@dataclass
class ArticleOmissions:
    article_id: int
    pairs: int = 0
    # 每段（回执序号）模型说漏掉、并且能在原文里找到的英文。
    findings: dict[int, list[str]] = field(default_factory=dict)
    # 模型摘的原文在这一段里找不到，丢掉了。
    unverified: int = 0
    # 回答收不成 JSON、被接口按内容拒（HTTP 400）的批。这几批没核，不算进漏句率的分母。
    failed_batches: int = 0
    failed_pairs: int = 0
    usage: Usage = field(default_factory=Usage)
    skipped: str = ""

    def as_dict(self, pairs_by_ordinal: dict[int, Pair] | None = None) -> dict[str, Any]:
        items = []
        for ordinal, quotes in sorted(self.findings.items()):
            item: dict[str, Any] = {"ordinal": ordinal, "missing": quotes}
            pair = (pairs_by_ordinal or {}).get(ordinal)
            if pair is not None:
                item["zh"] = pair.zh[:QUOTE_CHARS]
            items.append(item)
        return {"articleId": self.article_id, "pairs": self.pairs, "checked": self.checked,
                "flagged": len(self.findings), "findings": items, "unverified": self.unverified,
                "failedBatches": self.failed_batches, "usage": self.usage.as_dict(), "skipped": self.skipped}

    @property
    def checked(self) -> int:
        return self.pairs - self.failed_pairs


def receipt_pairs(provenance: dict | None) -> list[Pair]:
    """回执里逐段的英文和中文。没译出来、先放英文的段和没有英文字母的段（图、代码、纯数字）不送。"""
    if not isinstance(provenance, dict):
        return []
    gaps = set(untranslated_slots(provenance))
    pairs = []
    for item in provenance.get("segments") or []:
        try:
            ordinal = int(item["ordinal"])
            en, zh = _text(item["source"]), _text(item["target"])
        except (KeyError, TypeError, ValueError):
            continue
        if ordinal in gaps or not _LETTER.search(en) or not zh:
            continue
        pairs.append(Pair(ordinal, en, zh))
    return pairs


def batches(pairs: list[Pair], *, max_pairs: int = MAX_PAIRS, max_chars: int = MAX_CHARS) -> list[list[Pair]]:
    """按原文顺序分批；一段比上限还长就自己一批。"""
    out: list[list[Pair]] = []
    current: list[Pair] = []
    size = 0
    for pair in pairs:
        if current and (len(current) >= max_pairs or size + len(pair.en) > max_chars):
            out.append(current)
            current, size = [], 0
        current.append(pair)
        size += len(pair.en)
    if current:
        out.append(current)
    return out


def _quote_in(quote: str, en: str) -> bool:
    needle = _norm(quote)
    return len(needle) >= 3 and needle in _norm(en)


def parse_findings(data: Any, batch: list[Pair]) -> tuple[dict[int, list[str]], int]:
    """模型回答 → {回执序号: [漏掉的英文]}，以及找不到出处、丢掉的摘句条数。"""
    found: dict[int, list[str]] = {}
    unverified = 0
    rows = data.get("t") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        index = row.get("i")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(batch):
            continue
        pair = batch[index]
        quotes = row.get("missing")
        for quote in quotes if isinstance(quotes, list) else []:
            if not isinstance(quote, str) or not quote.strip():
                continue
            if not _quote_in(quote, pair.en):
                unverified += 1
                continue
            kept = found.setdefault(pair.ordinal, [])
            text = quote.strip()[:QUOTE_CHARS]
            if text not in kept:
                kept.append(text)
    return found, unverified


def service_failure(error: BaseException) -> bool:
    """断线、限流、欠费、没配密钥、要重新登录：后面的批问了也一样，整条命令停下。"""
    if isinstance(error, (LLMTransportError, LLMConfigError, XaiLoginRequiredError)):
        return True
    return isinstance(error, LLMHTTPError) and error.status not in {400, 413, 422}


def review_pairs(article_id: int, pairs: list[Pair], *, data_dir: Path | None = None) -> ArticleOmissions:
    """逐批问模型。一批的回答用不了就记下、接着核下一批；服务那头的错误照样抛出去，由命令行停下。"""
    result = ArticleOmissions(article_id, pairs=len(pairs))
    for batch in batches(pairs):
        try:
            reply = chat_json(
                system=SYSTEM,
                user={"pairs": [{"i": index, "en": pair.en, "zh": pair.zh} for index, pair in enumerate(batch)]},
                purpose="bilingual_omissions",
                subject=f"article-{article_id}",
                temperature=0.0,
                data_dir=data_dir,
            )
        except (RuntimeError, ValueError) as exc:
            if service_failure(exc):
                raise
            result.failed_batches += 1
            result.failed_pairs += len(batch)
            continue
        result.usage.add(reply.usage)
        found, unverified = parse_findings(reply.data, batch)
        result.unverified += unverified
        for ordinal, quotes in found.items():
            result.findings.setdefault(ordinal, []).extend(quotes)
    return result


def load_receipt(conn, article_id: int) -> dict | None:
    row = conn.execute(
        "SELECT provenance FROM article_translations WHERE article_id = %s AND lang = 'zh'", (article_id,),
    ).fetchone()
    return (row or {}).get("provenance") if row else None


__all__ = [
    "MAX_CHARS",
    "MAX_PAIRS",
    "SYSTEM",
    "ArticleOmissions",
    "Pair",
    "Usage",
    "batches",
    "load_receipt",
    "parse_findings",
    "receipt_pairs",
    "review_pairs",
    "service_failure",
]

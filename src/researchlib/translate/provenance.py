"""有序源段与译段回执。数量相同不是语义校验；这里只验证身份、事实锚点和保真。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

from researchlib.translate.segments import iter_segments, splice_segments, split_sentence_paragraphs, top_level_blocks

SCHEMA_VERSION = 1
CLEANER_VERSION = 1
SEGMENTER_VERSION = 1
PROMPT_VERSION = 2
PIPELINE = {"cleaner": CLEANER_VERSION, "segmenter": SEGMENTER_VERSION, "prompt": PROMPT_VERSION}
_PARA_ID = re.compile(r'\s+id="p-\d+"')
_TAG = re.compile(r"<[^>]*>")
# Q3、FY2026、H100 中的数字也属于事实；不能因前一字符是拉丁字母而漏掉。
_NUMBER = re.compile(r"(?<!\d)[-−+]?\d[\d,]*(?:\.\d+)?(?:%|％)?")


class AlignmentError(RuntimeError):
    """拒绝覆盖好版本；错误码不带原文、地址或模型响应。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"translation alignment: {code}")


def digest(value: object) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonical_html(value: str) -> str:
    """去掉页面临时锚点；不忽略链接、数字、出现次数或标签层级。"""
    value = _PARA_ID.sub("", value or "").strip()
    # Markdown 在相邻块之间补的换行不是源内容；行内标签间的空格和代码原样保留。
    blocks = r"p|h[1-6]|blockquote|li|ul|ol|dl|dt|dd|table|thead|tbody|tr|pre|figure|div"
    return re.sub(rf"(</(?:{blocks})>)[ \t\r\n]+(?=<(?:{blocks})\b)", r"\1", value)


class Resources(HTMLParser):
    def __init__(self, value: str):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.media: list[tuple] = []
        self.codes: list[str] = []
        self.code_depth = 0
        self.code: list[str] = []
        self.text: list[str] = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag in {"img", "source", "track", "video"}:
            self.media.append((tag, tuple(sorted((k, v) for k, v in attrs.items() if k != "id"))))
        if tag in {"pre", "code"}:
            if not self.code_depth:
                self.code = []
            self.code_depth += 1

    def handle_endtag(self, tag):
        if tag in {"pre", "code"} and self.code_depth:
            self.code_depth -= 1
            if not self.code_depth:
                self.codes.append("".join(self.code))

    def handle_data(self, data):
        if self.code_depth:
            self.code.append(data)
        else:
            self.text.append(data)


def _number_text(value: str) -> str:
    text = " ".join(Resources(value).text)
    # Q1–Q4 常译为第一至第四季度；仅规范这个明确的财务序数，不把普通中文词汇当数字。
    text = re.sub(r"(?<![零〇一二三四五六七八九十百千万亿两])第?([一二三四])季度",
                  lambda match: str("一二三四".index(match[1]) + 1) + "季度", text)
    return text


def numeric_anchors(value: str) -> list[str]:
    return [m.group().replace(",", "").replace("−", "-").replace("％", "%")
            for m in _NUMBER.finditer(_number_text(value))]


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_MONTH_NUMBERS = {name[:3].lower(): index for index, name in enumerate(_MONTHS, 1)}
_MONTH_PATTERN = "|".join((*_MONTHS, *(month[:3] for month in _MONTHS), "Sept"))
_POSITIONAL_NUMBER = re.compile(
    rf"(?P<month>{_MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*|\s+)(?P<year>\d{{4}})\b"
    r"|(?P<zh_year>\d{4})\s*年\s*(?P<zh_month>\d{1,2})\s*月\s*(?P<zh_day>\d{1,2})\s*日"
    r"|百分之(?P<zh_percent>百|一百|[一二三四五六七八九]?十[一二三四五六七八九]?|[零〇一二三四五六七八九])"
    r"(?![零〇一二三四五六七八九十百千万亿两点])"
    r"|(?<!\d)(?P<amount>[-−+]?\d[\d,]*(?:\.\d+)?)(?:\s*(?P<unit>"
    r"trillion\b|billion\b|million\b|thousand\b|万亿|亿|万|千|percent\b|%|％))?", re.I,
)
_SCALE_EXPONENTS = {"thousand": 3, "million": 6, "billion": 9, "trillion": 12,
                    "千": 3, "万": 4, "亿": 8, "万亿": 12}


def _positional_numbers(value: str) -> list[tuple]:
    """只读展示识别明确等值的单位、百分比和完整日期；不用于新稿回执。"""
    facts = []
    for match in _POSITIONAL_NUMBER.finditer(_number_text(value)):
        if match["month"] or match["zh_year"]:
            year = int(match["year"] or match["zh_year"])
            month = (_MONTH_NUMBERS[match["month"][:3].lower()]
                     if match["month"] else int(match["zh_month"]))
            day = int(match["day"] or match["zh_day"])
            try:
                facts.append(("date", date(year, month, day)))
            except ValueError:
                # 非法日期不能让展示崩溃，也不能与另一种写法碰巧等值。
                facts.append(("invalid-date", match.group()))
        elif match["zh_percent"]:
            word = match["zh_percent"]
            digits = "零一二三四五六七八九"
            if word in {"百", "一百"}:
                amount = 100
            elif "十" in word:
                tens, ones = word.split("十")
                amount = (digits.index(tens) if tens else 1) * 10 + (digits.index(ones) if ones else 0)
            else:
                amount = 0 if word == "〇" else digits.index(word)
            facts.append(("percent", Decimal(amount)))
        else:
            amount = Decimal(match["amount"].replace(",", "").replace("−", "-"))
            unit = (match["unit"] or "").lower()
            if unit in {"%", "％", "percent"}:
                facts.append(("percent", amount))
            else:
                # 移动十进制指数，不做受当前 Decimal 精度影响的乘法，超长金额也不能碰撞。
                sign, digits, exponent = amount.as_tuple()
                facts.append(("number", Decimal((sign, digits, exponent + _SCALE_EXPONENTS.get(unit, 0)))))
    return facts


def positional_numbers_conflict(sources: Sequence[str], targets: Sequence[str]) -> bool:
    """先识别明确等值的本地化，再按槽位、顺序和重数比较；正确段数不能抵消错误段。"""
    if len(sources) != len(targets):
        return True
    return any(_positional_numbers(en) != _positional_numbers(zh) for en, zh in zip(sources, targets, strict=True))


def leaf_problem(source: str, target: str) -> str | None:
    """数字按请求约定原序保留；资源逐段、带重数比较，不能拿别处的资源抵消。"""
    before, after = Resources(source), Resources(target)
    if before.links != after.links:
        return "links"
    if before.media != after.media:
        return "media"
    if before.codes != after.codes:
        return "code"
    if numeric_anchors(source) != numeric_anchors(target):
        return "numbers"
    if re.search(r"⟦\d+⟧", target):
        return "placeholder"
    return None


def materialize(value: str) -> str:
    """把虚拟 br／长句叶子落成实际节点，保留非译内容和容器，读写共用。"""
    entries = iter_segments(value)
    result = splice_segments(value, {i: item.html for i, item in enumerate(entries)})
    if len(iter_segments(result)) != len(entries):
        raise AlignmentError("materialize-count")
    # 只容许空白格式变化；代码、资源以及文字顺序／重数必须都在。
    before, after = Resources(value), Resources(result)
    def plain(text):
        return re.sub(r"\s+", "", html.unescape(_TAG.sub("", text)))
    if plain(value) != plain(result) or (before.links, before.media, before.codes) != (
        after.links, after.media, after.codes
    ):
        raise AlignmentError("materialize-content")
    return result


def source_manifest(source: str) -> dict:
    source = canonical_html(materialize(source))
    entries = []
    for top, block in enumerate(top_level_blocks(source)):
        for local, segment in enumerate(iter_segments(block)):
            ordinal = len(entries)
            source_hash = digest(canonical_html(segment.html))
            entries.append({
                "id": f"s-{ordinal}-{source_hash[:16]}", "ordinal": ordinal, "path": [top, local],
                "source_hash": source_hash, "source": segment.html,
            })
    return {
        "schema": SCHEMA_VERSION, "pipeline": dict(PIPELINE), "source_snapshot": source,
        "source_fingerprint": digest({"pipeline": PIPELINE, "source": source, "segments": entries}),
        "segments": entries,
    }


def _skeleton(value: str) -> str:
    return canonical_html(splice_segments(value, {
        i: f"<p>TXSLOT{i}</p>" for i, _item in enumerate(iter_segments(value))
    }))


def receipt(manifest: dict, translated: str, *, model: str = "") -> dict:
    """只有每个槽位、资源和完整外壳都通过，才签发可持久化回执。"""
    targets = iter_segments(translated)
    if len(targets) != len(manifest["segments"]):
        raise AlignmentError("receipt-count")
    if _skeleton(manifest["source_snapshot"]) != _skeleton(translated):
        raise AlignmentError("receipt-skeleton")
    entries = []
    for source, target in zip(manifest["segments"], targets, strict=True):
        problem = leaf_problem(source["source"], target.html)
        if problem:
            raise AlignmentError(f"slot-{source['ordinal']}-{problem}")
        entries.append({**source, "target": target.html, "target_hash": digest(canonical_html(target.html))})
    return {**manifest, "segments": entries, "target_hash": digest(canonical_html(translated)), "model": model,
            "semantic_status": "not-reviewed"}


@dataclass(frozen=True)
class MappingState:
    status: str
    reason: str = ""


def verify(source: str, translated: str, provenance: dict | None) -> MappingState:
    if provenance is None or provenance == {}:
        return MappingState("legacy", "missing-provenance")
    if not isinstance(provenance, dict):
        return MappingState("incompatible", "invalid-receipt")
    if provenance.get("schema") != SCHEMA_VERSION or provenance.get("pipeline") != PIPELINE:
        return MappingState("incompatible", "pipeline-version")
    try:
        current = source_manifest(source)
        if current["source_fingerprint"] != provenance.get("source_fingerprint"):
            return MappingState("incompatible", "source-fingerprint")
        expected = receipt(current, translated, model=provenance.get("model", ""))
        if (expected["segments"] != provenance.get("segments")
                or expected["target_hash"] != provenance.get("target_hash")):
            return MappingState("incompatible", "target-receipt")
    except (AlignmentError, TypeError, KeyError, ValueError):
        return MappingState("incompatible", "invalid-receipt")
    return MappingState("mapped")


def reconstruction(source: str, provenance: dict) -> str | None:
    """只生成候选，不写库；必须能恢复到原回执的完整目标校验值。"""
    try:
        candidate = splice_segments(provenance["source_snapshot"], {
            item["ordinal"]: item["target"] for item in provenance["segments"]
        })
        if verify(source, candidate, provenance).status == "mapped":
            return candidate
    except (KeyError, TypeError, AlignmentError):
        pass
    return None


def anchored_legacy_reflow(source: str, translated: str) -> tuple[str, str] | None:
    """旧粗段可无损按句展开时，用逐段有序唯一的数字／资料锚点核对，不签发回执。"""
    before = (materialize(source), materialize(translated))
    candidates = tuple(materialize(split_sentence_paragraphs(value)) for value in before)
    left, right = (iter_segments(value) for value in candidates)
    if len(left) <= 1 or len(left) != len(right):
        return None
    anchors = []
    for en, zh in zip(left, right, strict=True):
        facts = Resources(en.html)
        key = (tuple(numeric_anchors(en.html)), tuple(facts.links))
        if not any(key) or leaf_problem(en.html, zh.html):
            return None
        anchors.append(key)
    if len(set(anchors)) != len(anchors):
        return None
    for old, new in zip(before, candidates, strict=True):
        old_facts, new_facts = Resources(old), Resources(new)
        def plain(value):
            return re.sub(r"\s+", "", html.unescape(_TAG.sub("", value)))
        if (plain(old) != plain(new)
                or (old_facts.links, old_facts.media, old_facts.codes)
                != (new_facts.links, new_facts.media, new_facts.codes)):
            return None
    return candidates

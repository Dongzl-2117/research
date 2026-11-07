"""有序源段与译段回执。数量相同不是语义校验；这里只验证身份、事实锚点和保真。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from researchlib.translate.numbers import digits_hide_scale_change, same_numeric_facts
from researchlib.translate.segments import iter_segments, splice_segments, split_sentence_paragraphs, top_level_blocks

SCHEMA_VERSION = 1
CLEANER_VERSION = 1
SEGMENTER_VERSION = 1
PROMPT_VERSION = 2
PIPELINE = {"cleaner": CLEANER_VERSION, "segmenter": SEGMENTER_VERSION, "prompt": PROMPT_VERSION}
# 只给失败重试用，不写进回执。改数字或链接还原规则时加一，旧拒绝才会重新进队列。
# 不要放进 PIPELINE：那会把已经核对过的译文整篇标成不兼容。
# 3：下一季度不是 Q1；财季、中文数字、6/10、up from 和可移动的四位年份按同一事实放行。
# 4：``November, 2014`` 就是 2014年11月；twice 是两次；原文没写个数时，多出来的「两种 / 第一步」可以不计入。
# 5：金额尾零相同；900多万是 900 万；钟点带上午/下午，没写的只和上午相容；
# 周、月、天、小时分开记，可以挪到金额前面；比例两端之间不能再夹别的数字。
# 6：a week、糟糕的一周、过去一周、week-old、各一周和 8 天是同一段时间；
# couple/few/几周可以写成两周，写明 two 不能收成几。每月、每年的标价不是「每 X 美元就有 Y 美元」。
# 7：写明个数的 every two weeks 和每两周都是两周间隔；没写个数的每周仍不是硬事实。
# one day it will、有一天、有朝一日不是量出来的一天。
# 8：couple 可以写成两、2 或几；few 只等于几，不能写成确切的两。
# one to seven days 对 1至7天；24 小时允许空格；a long one 回指本周；
# late 90s 对 20世纪90年代末；800 data points on 100 million 可以换序；就产生也是比例。
ALIGNMENT_VERSION = 8
_PARA_ID = re.compile(r'\s+id="p-\d+"')
_TAG = re.compile(r"<[^>]*>")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-−+]?\d[\d,]*(?:\.\d+)?(?:%|％)?")


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


def numeric_anchors(value: str) -> list[str]:
    facts = Resources(value)
    return [m.group().replace(",", "").replace("−", "-").replace("％", "%")
            for m in _NUMBER.finditer(" ".join(facts.text))]


def positional_numbers_conflict(sources: Sequence[str], targets: Sequence[str]) -> bool:
    """等长叶子仍不能硬配的两种数字情况。

    同一批数字换了段落，或过半段落的数字列表对不上，说明位置不可信。
    少数段落把 300,000 写成 30万、把 69.1 写成 691、把 100% 写成百分之百，
    全集已经不同，这是译法，不能因此把整页退成两列。
    """
    from collections import Counter

    if len(sources) != len(targets):
        return True
    left = [tuple(numeric_anchors(item)) for item in sources]
    right = [tuple(numeric_anchors(item)) for item in targets]
    if left == right or not left:
        return False
    tokens = Counter(anchor for row in left for anchor in row)
    if tokens and tokens == Counter(anchor for row in right for anchor in row):
        return True
    mismatches = sum(en != zh for en, zh in zip(left, right, strict=True))
    return mismatches * 2 > len(left)


def _resource_hrefs(facts: Resources) -> list[str]:
    """没有地址的书签（旧文 ``<a name>``）不是资料链接。"""
    return [href for href in facts.links if href]


def leaf_problem(source: str, target: str) -> str | None:
    """数字按事实原序保留；资源逐段、带重数比较，不能拿别处的资源抵消。"""
    before, after = Resources(source), Resources(target)
    if _resource_hrefs(before) != _resource_hrefs(after):
        return "links"
    if before.media != after.media:
        return "media"
    if before.codes != after.codes:
        return "code"
    # 字符级相同通常直接过，避免把版本号约成小数。两边数字串一样但数量级不同
    # （10 billion 写成 10亿）仍然是另一个数。数字串不同才走日期、单位和财季的等值。
    if numeric_anchors(source) == numeric_anchors(target):
        if digits_hide_scale_change(source, target):
            return "numbers"
    elif not same_numeric_facts(source, target):
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

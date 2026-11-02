"""译段的可验证边界：有序身份、事实归属和处理版本，不把结构通过当语义证明。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from html.parser import HTMLParser

from researchlib.translate.segments import iter_segments

# 清洗、渲染或切段语义改变时递增对应版本；旧稿只告警，不因此加入付费队列。
PROCESSING_VERSION = {"cleaner": 1, "renderer": 1, "segmenter": 1, "protocol": 1}
_PARA_ID = re.compile(r'''\s+id=["']p-\d+["']''')
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class SegmentFacts(HTMLParser):
    """按段保存数字及资源顺序；同一地址出现两次也必须保留两次。"""

    def __init__(self, document: str):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.resources: list[tuple[str, str]] = []
        self.code: list[str] = []
        self.structure: list[tuple] = []
        self._code_depth = 0
        self.feed(document or "")
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"a", "img", "source", "track", "video", "audio"}:
            for name in ("href", "src", "poster"):
                if attrs.get(name):
                    self.resources.append((f"{tag}:{name}", attrs[name]))
        if tag in {"table", "tr", "td", "th", "ul", "ol", "li", "dl", "dt", "dd", "pre", "code"}:
            self.structure.append((tag, attrs.get("start"), attrs.get("value"), attrs.get("rowspan"),
                                   attrs.get("colspan"), "reversed" in attrs))
        if tag in {"pre", "code"}:
            self._code_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in {"pre", "code"}:
            self._code_depth = max(0, self._code_depth - 1)

    def handle_data(self, data):
        self.text.append(data)
        if self._code_depth:
            self.code.append(data)

    @property
    def numbers(self) -> Counter:
        return Counter(_NUMBER.findall("".join(self.text)))


def segment_problem(source: str, translated: str) -> str | None:
    """拒绝明显的数字/链接/图片/代码张冠李戴；不声称能证明任意译文的语义。"""
    left, right = SegmentFacts(source), SegmentFacts(translated)
    if left.numbers != right.numbers:
        return "numbers"
    if left.resources != right.resources:
        return "resources"
    if left.code != right.code:
        return "code"
    return None


def repeat_identity(document: str) -> tuple:
    """只允许两侧各自文字、资源和有意义结构都相同的整对折叠。"""
    facts = SegmentFacts(document)
    return (re.sub(r"\s+", " ", "".join(facts.text)).strip(), tuple(facts.resources),
            tuple(facts.code), tuple(facts.structure))


def fingerprint(document: str) -> str:
    # p-N 是读取时加的锚点，不是正文身份。其余文字、顺序、属性都参与校验。
    return hashlib.sha256(_PARA_ID.sub("", document or "").encode()).hexdigest()


def source_manifest(document: str) -> list[dict]:
    occurrences: Counter = Counter()
    manifest = []
    for index, segment in enumerate(iter_segments(document)):
        digest = fingerprint(segment.html)
        occurrence = occurrences[digest]
        occurrences[digest] += 1
        facts = SegmentFacts(segment.html)
        manifest.append({"id": f"s{index}-{digest[:16]}-{occurrence}", "index": index,
                         "sourceHash": digest, "occurrence": occurrence,
                         "resources": [list(item) for item in facts.resources],
                         "numbers": dict(facts.numbers)})
    return manifest


def make_provenance(original: str, translated: str) -> dict:
    sources, targets = iter_segments(original), iter_segments(translated)
    if len(sources) != len(targets):
        raise RuntimeError("translation alignment: manifest count")
    manifest = source_manifest(original)
    for entry, source, target in zip(manifest, sources, targets, strict=True):
        problem = segment_problem(source.html, target.html)
        if problem:
            raise RuntimeError(f"translation alignment: segment {entry['index']} {problem}")
        entry["targetHash"] = fingerprint(target.html)
    return {"version": PROCESSING_VERSION.copy(), "sourceHash": fingerprint(original),
            "translationHash": fingerprint(translated), "segments": manifest}


def provenance_problem(original: str, translated: str, provenance: object) -> str | None:
    if provenance is None:
        return "legacy-unverified"
    if not isinstance(provenance, dict):
        return "invalid-provenance"
    if provenance.get("version") != PROCESSING_VERSION:
        return "processing-version"
    if provenance.get("sourceHash") != fingerprint(original):
        return "canonical-input-changed"
    if provenance.get("translationHash") != fingerprint(translated):
        return "translation-changed"
    try:
        expected = make_provenance(original, translated)
    except RuntimeError:
        return "mapping-invalid"
    if provenance != expected:
        return "manifest-invalid"
    return None

"""有序源段与译段回执。数量相同不是语义校验；这里只验证身份、事实锚点和保真。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from researchlib.translate.numbers import digits_hide_scale_change, hard_fact_counts, same_numeric_facts
from researchlib.translate.segments import iter_segments, splice_segments, split_sentence_paragraphs, top_level_blocks

SCHEMA_VERSION = 1
CLEANER_VERSION = 1
SEGMENTER_VERSION = 1
# 原文怎么清洗、怎么切段。回执上记的和这里不一样，回执就作废：页面分开展示，旧段也不复用。
PIPELINE = {"cleaner": CLEANER_VERSION, "segmenter": SEGMENTER_VERSION}
# 译制提示词第几版，记在回执的 prompt_versions 里，不进 PIPELINE：换提示词不改原文怎么切，
# 已经签过的回执照样逐段对照。11-09 以前的回执把它写在 pipeline 里（"prompt": 2），比对时不看这一项。
# 只有上一份回执是这一版提示词译的，再译时才直接复用没变的段（service.translate_article）。
# 2：11-09 以前。3：讲中文怎么写（不夹破折号、长句拆短、习语按意思译、全文一种叫法），见 docs/translate.md「提示词」。
PROMPT_VERSION = 3
# 只给失败重试用，不写进回执。改数字或链接还原规则时加一，旧拒绝才会重新进队列。
# 不要放进 PIPELINE：那会把已经核对过的译文整篇标成不兼容。
# 3：下一季度不是 Q1；财季、中文数字、6/10、up from 和可移动的四位年份按同一事实放行。
# 4：``November, 2014`` 就是 2014年11月；twice 是两次；原文没写个数时，多出来的「两种 / 第一步」可以不计入。
# 5：金额尾零相同；900多万是 900 万；钟点带上午/下午，没写的只和上午相容；
# 周、月、天、小时分开记，可以挪到金额前面；比例两端之间不能再夹别的数字。
# 6：a week、糟糕的一周、过去一周、week-old、各一周和 8 天是同一段时间；
# couple/few/几周可以写成两周，写明 two 不能收成几。每月、每年的标价不是「每 X 美元就有 Y 美元」。
# 7：数字对不上的段不再让整篇作废：同一批里这几段补译一次，还对不上就照样写入，记进回执的 number_review。
# 数字和同一批另一段原文正好一致（串段）仍整批重译。版本 6 及以前因数字被拒的文章重新进队列。
# 8：长段按句切开会丢掉或多出链接（句末紧贴的空锚点、链接文字里的缩写名）时整段不切。
# 这类文章以前在 materialize 就拒收（materialize-content），现在能译，重新进队列。
# 9：一段出错不再让整篇作废。身份、链接、占位符一直对不上或串段的那几段先放英文，序号记进回执的
# untranslated，别的段照常入库（complete 为假）；请求断线时已经译好的段也先存下。
# 下次只补这几段：原文没变的段按原文身份（reuse_key）复用已有译文，不再重译。
# 10：判串段时，另一段的数这一段原文里全都有（两段都写 January 2007、只有 F1 这种夹在词里的数）
# 就不拿来比，免得把「一千」「第二天」这种译法判成串段、整段先放英文。因此先放英文的段重新进队列。
ALIGNMENT_VERSION = 10
_PARA_ID = re.compile(r'\s+id="p-\d+"')
_SRC_PARA_ATTR = re.compile(r'\s+data-src-para="\d+"')
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


def structure_problem(source: str, target: str) -> str | None:
    """链接、图片、代码和占位符逐段、带重数比较，不能拿别处的资源抵消。

    这几样少一个，这段就没法原位写回，译文整批重来。
    """
    before, after = Resources(source), Resources(target)
    if _resource_hrefs(before) != _resource_hrefs(after):
        return "links"
    if before.media != after.media:
        return "media"
    if before.codes != after.codes:
        return "code"
    if re.search(r"⟦\d+⟧", target):
        return "placeholder"
    return None


def numbers_agree(source: str, target: str) -> bool:
    """数字按事实原序保留。

    字符级相同通常直接过，避免把版本号约成小数。两边数字串一样但数量级不同
    （10 billion 写成 10亿）仍然是另一个数。数字串不同才走日期、单位和财季的等值。
    """
    if numeric_anchors(source) == numeric_anchors(target):
        return not digits_hide_scale_change(source, target)
    return same_numeric_facts(source, target)


def leaf_problem(source: str, target: str) -> str | None:
    """结构和数字一起核对。

    译制写入不再因为一段数字对不上整篇作废（见 ``receipt`` 和 ``misplaced_slot``）。
    旧粗段按句展开（``anchored_legacy_reflow``）没有回执，只能拿数字确认对应，仍用这一条。
    """
    problem = structure_problem(source, target)
    if problem:
        return problem
    return None if numbers_agree(source, target) else "numbers"


def misplaced_slot(sources: Sequence[str], targets: Sequence[str], flagged: Sequence[int]) -> int | None:
    """数字没对上的一段，若和同一批另一段原文的数字正好一致，就是串段，不是译法不同。

    返回第一个串段的下标，没有返回 None。另一段原文没有必须对上的数字时不拿来比，
    免得「都没有数字」互相对上。另一段的数这一段原文里全都有时也不拿来比：两段都写着
    January 2007，或者那一段只有 F1 这种夹在词里的数，译文对上它，同样可能只是这一段
    把 the next day 写成「第二天」、把 a thousand 写成「一千」，数字分不出是不是串了段，
    交给待核（number_review）。只有译文对上的那一段带着这一段没有的数，才算串段。
    """
    from collections import Counter

    if not flagged:
        return None
    hard = [hard_fact_counts(source) for source in sources]
    for index in flagged:
        own = Counter(numeric_anchors(sources[index]))
        if own and own == Counter(numeric_anchors(targets[index])):
            # 数字还是这一段自己的，只是换了先后（「在20个国家有1000名员工」）。
            continue
        for other, source in enumerate(sources):
            if other == index or not hard[other] or not (hard[other] - hard[index]):
                continue
            if numbers_agree(source, targets[index]):
                return index
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


def pipeline_matches(recorded: object) -> bool:
    """回执上记的清洗、切段版本和现在一样。11-09 以前的回执多记一项 prompt，不算在内。"""
    if not isinstance(recorded, dict):
        return False
    return {key: value for key, value in recorded.items() if key != "prompt"} == PIPELINE


def prompt_versions(provenance: dict | None) -> list[int]:
    """回执里的段是按第几版提示词译的（复用过旧段的回执不止一版）。没有回执的旧稿返回空。"""
    if not isinstance(provenance, dict):
        return []
    found = provenance.get("prompt_versions")
    if found is None:
        pipeline = provenance.get("pipeline")
        legacy = pipeline.get("prompt") if isinstance(pipeline, dict) else None
        found = [] if legacy is None else [legacy]
    try:
        return sorted({int(item) for item in found})
    except (TypeError, ValueError):
        return []


def source_manifest(source: str, *, pipeline: dict | None = None) -> dict:
    """``pipeline`` 只在核对旧回执时传：指纹按回执自己记的那份流水线算。"""
    pipeline = dict(PIPELINE if pipeline is None else pipeline)
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
        "schema": SCHEMA_VERSION, "pipeline": pipeline, "source_snapshot": source,
        "source_fingerprint": digest({"pipeline": pipeline, "source": source, "segments": entries}),
        "segments": entries,
    }


def _skeleton(value: str) -> str:
    return canonical_html(splice_segments(value, {
        i: f"<p>TXSLOT{i}</p>" for i, _item in enumerate(iter_segments(value))
    }))


def shape_problem(manifest: dict, translated: str) -> str | None:
    """写回以后槽位数和外壳是不是还和原文一样。不一样返回 ``receipt-count`` 或 ``receipt-skeleton``。"""
    if len(iter_segments(translated)) != len(manifest["segments"]):
        return "receipt-count"
    if _skeleton(manifest["source_snapshot"]) != _skeleton(translated):
        return "receipt-skeleton"
    return None


def _signed_segments(manifest: dict, translated: str) -> list[dict]:
    """槽位数、外壳和每个槽位的资源都对上了，返回回执里逐段的那一份；对不上抛 AlignmentError。"""
    problem = shape_problem(manifest, translated)
    if problem:
        raise AlignmentError(problem)
    targets = iter_segments(translated)
    entries = []
    for source, target in zip(manifest["segments"], targets, strict=True):
        problem = structure_problem(source["source"], target.html)
        if problem:
            raise AlignmentError(f"slot-{source['ordinal']}-{problem}")
        entries.append({**source, "target": target.html, "target_hash": digest(canonical_html(target.html))})
    return entries


def receipt(
    manifest: dict, translated: str, *, model: str = "", untranslated: Sequence[int] = (),
    prompts: Sequence[int] = (),
) -> dict:
    """每个槽位的资源和完整外壳都通过，才签发可持久化回执。

    数字对不上的槽位照签，序号记进 ``number_review``。``untranslated`` 是这次没译出来、
    先放英文的槽位序号（那一段的 target 就是原文）。``prompts`` 是这篇里的段用过哪几版提示词，
    不传就是现在这一版。``verify`` 这三项都不比较：数字规则以后再改、补译了哪几段、换了提示词，
    已经入库的回执都不会因此变成不兼容、整页退成两栏。
    """
    entries = _signed_segments(manifest, translated)
    gaps = sorted(set(untranslated))
    review = [entry["ordinal"] for entry in entries
              if entry["ordinal"] not in gaps and not numbers_agree(entry["source"], entry["target"])]
    return {**manifest, "segments": entries, "target_hash": digest(canonical_html(translated)), "model": model,
            "semantic_status": "not-reviewed", "number_review": review, "untranslated": gaps,
            "prompt_versions": sorted({int(item) for item in prompts} or {PROMPT_VERSION})}


def untranslated_slots(provenance: dict | None) -> list[int]:
    """回执里没译出来、先放英文的槽位序号。没有回执或旧回执返回空。"""
    if not isinstance(provenance, dict):
        return []
    try:
        return sorted({int(item) for item in provenance.get("untranslated") or []})
    except (TypeError, ValueError):
        return []


def reuse_key(segment: str) -> str:
    """一段原文的身份，按它复用已有译文。

    和回执里的 ``source_hash`` 一样看整段 HTML（字、链接、图片、代码、标签），只是不看
    ``data-src-para`` 的编号：编号从全文开头数，前面多切出或少切出一段，后面每段的编号都变，原文其实没变。
    """
    return digest(canonical_html(_SRC_PARA_ATTR.sub("", segment or "")))


def reusable_targets(provenance: dict | None) -> dict[str, list[str]]:
    """回执里能原样再用的译文段，按 ``reuse_key`` 分组，组里保持原文里的先后。

    只收这一版清洗、切段签的回执里、自己校验值对得上、资源和原文一致的段；没译出来先放英文的段
    （``untranslated``）不收。没有回执的旧稿返回空，只能整篇重译。哪一版提示词译的不管：
    直接用还是只在重译失败时退回用，由译制按 ``prompt_versions`` 决定。
    """
    if not isinstance(provenance, dict):
        return {}
    if provenance.get("schema") != SCHEMA_VERSION or not pipeline_matches(provenance.get("pipeline")):
        return {}
    skip = set(untranslated_slots(provenance))
    found: dict[str, list[str]] = {}
    for item in provenance.get("segments") or []:
        try:
            if int(item["ordinal"]) in skip:
                continue
            source, target = item["source"], item["target"]
            if (digest(canonical_html(source)) != item["source_hash"]
                    or digest(canonical_html(target)) != item["target_hash"]
                    or structure_problem(source, target)):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        found.setdefault(reuse_key(source), []).append(target)
    return found


def unverified_numbers(provenance: dict | None) -> list[int]:
    """按现在的数字规则重算回执里数字没对上的槽位序号。没有回执的旧稿返回空。没译出来的段不算。"""
    if not isinstance(provenance, dict):
        return []
    found = []
    gaps = set(untranslated_slots(provenance))
    for item in provenance.get("segments") or []:
        try:
            if int(item["ordinal"]) not in gaps and not numbers_agree(item["source"], item["target"]):
                found.append(int(item["ordinal"]))
        except (KeyError, TypeError, ValueError):
            continue
    return found


@dataclass(frozen=True)
class MappingState:
    status: str
    reason: str = ""


def verify(source: str, translated: str, provenance: dict | None) -> MappingState:
    if provenance is None or provenance == {}:
        return MappingState("legacy", "missing-provenance")
    if not isinstance(provenance, dict):
        return MappingState("incompatible", "invalid-receipt")
    if provenance.get("schema") != SCHEMA_VERSION or not pipeline_matches(provenance.get("pipeline")):
        return MappingState("incompatible", "pipeline-version")
    try:
        # 指纹按回执自己记的流水线算：11-09 以前签的回执，pipeline 里多一项 prompt。
        current = source_manifest(source, pipeline=provenance["pipeline"])
        if current["source_fingerprint"] != provenance.get("source_fingerprint"):
            return MappingState("incompatible", "source-fingerprint")
        # 只重签逐段那一份，不重核数字：number_review 本来就不比（见 receipt），文章页每次打开都走这里，
        # 6 万字的 Stratechery 每段数字重核一遍要 0.46 秒，算完就扔。
        if (_signed_segments(current, translated) != provenance.get("segments")
                or digest(canonical_html(translated)) != provenance.get("target_hash")):
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

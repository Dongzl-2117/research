"""搜索的局部主体证据与词法降级先验。

这里不做领域同义词扩展。无向量时的语料中心性仅用于排列同公司的候选，不能证明主题相同，
尤其不能把中文查询和英文候选的共存当作跨语言理解。所有摘录均为原文子串。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from functools import lru_cache

_SENTENCE = re.compile(r"[^。！？!?\n]+(?:[。！？!?]|$)|[^\n]+$", re.M)
_EN_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_CLAUSE = re.compile(r"[^，,；;：:]+[，,；;：:]?")
_ANAPHOR = re.compile(r"^(?:它|其|该|这(?:家|个|部分)|这些|上述|it\b|its\b|they\b|their\b|this\b)", re.I)
# 否定的是归属/论述关系，不能将「没有降低毛利」之类的负面事实排除。
_DENIED_REFERENCE = re.compile(
    r"(?:不|未|没有|并未|不能|不可)[^。；;，,]{0,12}"
    r"(?:归给|归于|属于|认定|说明|证明|分析|讨论|描述|等同|视为)[^。；;，,]{0,12}$"
    r"|(?:not|never)\s+(?:about|attributed\s+to|evidence\s+for)\s*$", re.I,
)
_NOMINAL = re.compile(r"^[的\s]*(?:[。！？!?，,；;：:]|$|(?:是|为|只是|仅是|只作为|仅作为|只在)|is\b|was\b)", re.I)
_TOKEN = re.compile(r"[a-z][a-z0-9]+|[㐀-鿿]{2,}", re.I)


@dataclass
class Evidence:
    text: str
    start: int
    end: int
    companies: frozenset[int]
    confidence: float
    basis: str
    context: str = ""
    prior: float = 0.0


class LocalMatcher:
    """一次查询内复用主体识别；同块会用于句子、先验和向量边界，不重复扫描全公司别名。"""

    def __init__(self, matcher):
        self.spans = lru_cache(maxsize=16000)(matcher.spans)

    def count(self, text):
        return Counter(cid for _lo, _hi, cid in self.spans(text))


def sentences(text: str) -> list[tuple[int, int, str]]:
    out = []
    for match in _SENTENCE.finditer(text):
        offset = match.start()
        for part in _EN_SENTENCE.split(match.group()):
            start = text.find(part, offset, match.end())
            if start < 0:
                continue
            end = start + len(part)
            if part.strip():
                out.append((start, end, part))
            offset = end
    return out


def _reference(text: str, start: int, end: int, topic_forms: list[str]) -> str:
    if _DENIED_REFERENCE.search(text[:start]):
        return "denied"
    suffix = text[end:]
    # 「公司 + 查询题面」是一个名词短语；仅点名该短语、作标题或被引述，不等于陈述公司的事实。
    consumed = False
    while True:
        cleaned = suffix.lstrip(" 的\t\n")
        found = next((f for f in topic_forms if cleaned.casefold().startswith(f.casefold())), None)
        if not found:
            break
        consumed = True
        suffix = cleaned[len(found):]
    if re.match(r"^\s*(?:[。！？!?，,；;：:]|$)", suffix):
        return "label"
    if consumed and _NOMINAL.match(suffix):
        # 「商业模式是……」可能是定义，也可能只是在命名题目。无模型不能把两者当作已判定。
        return "nominal"
    return ""


def local_evidence(row, matcher, focus, topic_forms, *, previous=None) -> list[Evidence]:
    """名字和谓词必须在局部连起来；混合主体的分句不猜归属，否定引用不提供主体依据。"""
    text = row["body"] or ""
    pieces = sentences(text)
    title_ids = set(matcher.count(row.get("title") or ""))
    heading_ids = set(matcher.count(row.get("heading") or ""))
    body_spans = list(matcher.spans(text))
    block_ids = {cid for _lo, _hi, cid in body_spans}
    contradicted = bool(block_ids - focus) or any(
        _reference(text, lo, hi, topic_forms) == "denied" for lo, hi, cid in body_spans if cid in focus
    )
    prior_ids, prior_text = set(), ""
    if previous:
        prior_ids = set(matcher.count(previous["body"] or ""))
        prior_text = previous["body"] or ""
    # 裸名称、题面名词短语和被否定的归属不能再为同块的任意新句子继承主体。
    has_claim = any(cid in focus and not _reference(text, lo, hi, topic_forms)
                    for lo, hi, cid in body_spans)
    same_block = block_ids & focus if has_claim and not block_ids - focus else set()
    out = []
    active = set()
    for start, end, sentence in pieces:
        # 多主体时按分句绑定；单主体时保留完整句，避免把因果从中间截断。
        sentence_ids = {cid for lo, hi, cid in body_spans if start <= lo and hi <= end}
        chunks = list(_CLAUSE.finditer(sentence)) if sentence_ids - focus else []
        units = [(start + c.start(), start + c.end(), c.group()) for c in chunks] or [(start, end, sentence)]
        for lo, hi, unit in units:
            spans = [(a - lo, b - lo, cid) for a, b, cid in body_spans if lo <= a and b <= hi]
            named = {cid for _, _, cid in spans}
            own = named & focus
            foreign = named - focus
            references = {cid: _reference(unit, a, b, topic_forms) for a, b, cid in spans}
            denied = {cid for cid, reason in references.items() if reason}
            if foreign:
                active = foreign
                continue
            if own:
                active = own - denied
                if own <= denied:
                    # 仍可作为随后显式代词的上下文，但不给名词题面很高的主题分。
                    confidence = 0.2 if not contradicted and any(
                        references[cid] == "nominal" for cid in own
                    ) else 0.0
                    out.append(Evidence(unit, lo, hi, frozenset(own), confidence, "提及"))
                else:
                    out.append(Evidence(unit, lo, hi, frozenset(own - denied), 1.0, "正文"))
                continue
            stripped = unit.lstrip(" \t\n“\"（(")
            if heading_ids - focus or active - focus:
                # 局部章节/前句已改谈别家时，较远的标题和上块不能覆盖这个转场。
                continue
            if prior_ids - focus and not heading_ids & focus:
                continue
            if _ANAPHOR.match(stripped) and active:
                if not active - focus:
                    out.append(Evidence(unit, lo, hi, frozenset(active), 0.9, "本块上文"))
                continue
            if _ANAPHOR.match(stripped) and prior_ids and not prior_ids - focus:
                out.append(Evidence(unit, lo, hi, frozenset(prior_ids), 0.85, "上文", prior_text))
            elif heading_ids & focus and not heading_ids - focus:
                out.append(Evidence(unit, lo, hi, frozenset(heading_ids & focus), 0.8, "章节"))
            elif title_ids & focus and not title_ids - focus:
                out.append(Evidence(unit, lo, hi, frozenset(title_ids & focus), 0.55, "标题"))
            elif same_block:
                # 段内省略主语只提供较弱的候选依据，主题仍须由词法/语义检验。
                context = pieces[0][2] if start else ""
                out.append(Evidence(unit, lo, hi, frozenset(same_block), 0.4, "本块上文", context))
    return out


def _features(text, matcher, ignored):
    for lo, hi, _cid in sorted(matcher.spans(text), reverse=True):
        text = text[:lo] + " " + text[hi:]
    for form in ignored:
        text = re.sub(re.escape(form), " ", text, flags=re.I)
    tokens = set()
    for match in _TOKEN.finditer(text.casefold()):
        token = match.group()
        if token.isascii():
            if len(token) > 2 and token not in {"the", "and", "for", "that", "with", "this", "from", "its"}:
                tokens.add(token)
        else:
            tokens.update(token[i:i + 2] for i in range(len(token) - 1))
    return tokens


def evidence_windows(text: str, units: list[Evidence], *, chars: int = 500) -> list[Evidence]:
    """排名和展示使用相同的连续局部窗口，不能只给长英文机制的第一短句打分。"""
    windows = []
    for index, unit in enumerate(units):
        if not unit.confidence:
            continue
        end = unit.end
        for following in units[index + 1:]:
            if (following.confidence < 0.4 or following.companies != unit.companies
                    or text[end:following.start].strip() or following.end - unit.start > chars):
                break
            end = following.end
        windows.append(replace(unit, text=text[unit.start:end], end=end))
    return windows


def corpus_prior(evidence, matcher, ignored):
    """无模型时的弱先验：跨独立原文出现的内容优先于单篇重复堆字。不是主题相似度。"""
    docs = defaultdict(set)
    unique = set()
    features = {}
    for key, units in evidence.items():
        for unit in units:
            tokens = features.setdefault(unit.text, None)
            if tokens is None:
                tokens = features[unit.text] = _features(unit.text, matcher, ignored)
            if unit.confidence < 0.5:
                continue
            fingerprint = re.sub(r"\s+", "", unit.text).casefold()
            if fingerprint in unique:
                continue
            unique.add(fingerprint)
            docs[key[0]].update(tokens)
    df = Counter(token for tokens in docs.values() for token in tokens)
    weights = {token: math.log1p(count - 1) for token, count in df.items() if count > 1}
    for units in evidence.values():
        for unit in units:
            tokens = features[unit.text]
            central = sum(weights.get(t, 0.0) for t in tokens) / max(math.sqrt(len(tokens)), 1)
            information = min(len(tokens) / 28, 1.0)
            unit.prior = 0.15 * information + 0.85 * central / (central + 3)


def evidence_fingerprint(text: str) -> str:
    """只折叠相同原文证据。保留数字、标点和否定词，不能把近似但事实不同的文字当成转载。"""
    return re.sub(r"\s+", " ", text).strip().casefold()

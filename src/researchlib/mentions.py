"""公司别名匹配和提及深度。

深度规则见 docs/mentions.md。匹配不走分词：中文最长不重叠，英文按词边界。
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from researchlib.textutil import markdown_to_text, split_frontmatter, split_markdown_sections, strip_duplicate_title

DEPTH_TITLE = "title"
DEPTH_WHOLE = "whole"
DEPTH_SECTION = "section"
DEPTH_PASSING = "passing"
DEPTHS = (DEPTH_TITLE, DEPTH_WHOLE, DEPTH_SECTION, DEPTH_PASSING)

# 判断深度的规则改了就加一：应用启动时发现和上次重算时不一样，在后台把全部提及重算一遍。
MENTION_RULES_VERSION = "2"

# 无结构正文里，达到这个次数视为整篇都在谈。
UNSTRUCTURED_WHOLE_HITS = 3
# 有章节时，同一章达到这个次数也视为全文，而不是「单节」。
STRUCTURED_SECTION_WHOLE_HITS = 6

# 英文长文（Stratechery 一篇几千词、上百句）按句子比例判断，不按绝对次数：
# 拿苹果、谷歌来类比的句子在长文里随便就有三五句，按中文规则会全部升成「全文」。
# 正文字母少于这个数的短帖仍按中文规则数次数。
LATIN_MIN_LETTERS = 200
# 全文：不少于这么多句、而且不少于全部句子的这个比例在谈它。
LATIN_WHOLE_MIN = 3
LATIN_WHOLE_SHARE = 0.06
# 单节：连续这么多句里至少有这么多句在谈它（集中讲了一段），或者章节标题里点了名。
LATIN_WINDOW = 20
LATIN_WINDOW_HITS = 5
# 有三个以上章节、八成命中落在同一章：是那一章在谈，记单节。
LATIN_ONE_SECTION_SHARE = 0.8
# 文章的主角：开头这么多句里就点了名、全篇点名最多、不少于 LATIN_WHOLE_MIN 句，也算全文。
# 主角常用「the company」指代，按比例可能不够。
LATIN_LEAD = 10

# 这些短英文词区分大小写，避免打进普通单词。
CASE_SENSITIVE_LATIN = frozenset(
    {
        "Meta",
        "META",
        "LI",
        "JD",
        "MA",
        "V",
        "ARM",
        "AI",
        "BOE",
        "EVE",
        "SF",
        "COST",
        "Apple",
        "APPLE",
    }
)

DEPTH_LABELS = {
    DEPTH_TITLE: "标题",
    DEPTH_WHOLE: "全文",
    DEPTH_SECTION: "单节",
    DEPTH_PASSING: "一带而过",
}

# 类比、举例、顺带一提。这些句子里的公司名不拿来升级成「全文」或「单节」。
_INCIDENTAL = re.compile(
    r"(例如|比如|譬如|好比|就像|犹如|类似|举例|打个比方|作为例子|举个例子|顺带|顺便|提一句|说句题外话)"
)
_SENTENCE = re.compile(r"[^。！？!?\n；;]+[。！？!?\n；;]?")

# 英文里的举例、类比、顺带一提。只用在英文文章上，不碰中文的判断。
_LATIN_INCIDENTAL = re.compile(
    r"(?<![A-Za-z])(?:for example|for instance|e\.g\.|such as|just like|much like|similar to|similarly|"
    r"compared (?:to|with)|in contrast|by contrast|unlike|akin to|analogous to|reminiscent of|"
    r"the same way|by the way|as an aside|incidentally|à la)(?![A-Za-z])",
    re.IGNORECASE,
)
_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
# 句末标点，后面可以跟引号、括号，再跟空白。
_LATIN_END = re.compile(r"[.!?]+[\"'”’)\]]*\s+")
# 这些缩写后面的点不是句末：U.S. Steel、Mr. Cook、e.g. Apple。
_NO_BREAK_AFTER = frozenset(
    {"e.g", "i.e", "vs", "mr", "mrs", "ms", "dr", "inc", "corp", "co", "ltd", "jr", "sr", "st"}
    | {"u.s", "u.k", "u.n", "e.u"}
)
_HEADING_LINE = re.compile(r"\A#{1,6}[ \t]+(\S[^\n]*)")

# 公众号页脚和分享提示。收「微信」之前先把这些整段抹掉，免得几乎每篇都记成提到腾讯。
_PUBLISHER_NOISE = re.compile(
    r"微信扫一扫|在微信中打开|微信搜索公众号|微信搜索|打开微信|"
    r"分享到微信|分享到朋友圈|分享朋友圈|微信公众号|"
    r"长按识别二维码|识别图中二维码|请关注公众号|关注公众号|欢迎关注"
)


def scrub_publisher_noise(text: str) -> str:
    """把公众号模板字样换成等长空格，保留位置，匹配时就不会撞上里面的「微信」。"""
    if not text:
        return text

    def blank(match: re.Match[str]) -> str:
        return " " * (match.end() - match.start())

    return _PUBLISHER_NOISE.sub(blank, text)



class _Node:
    __slots__ = ("next", "fail", "out")

    def __init__(self) -> None:
        self.next: dict[str, _Node] = {}
        self.fail: _Node | None = None
        self.out: list[tuple[int, int]] = []


def is_cjk_alias(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def latin_casefold(text: str) -> bool:
    if text in CASE_SENSITIVE_LATIN:
        return False
    return not (len(text) <= 2 and text.isalpha())


def alias_key(text: str) -> str:
    if is_cjk_alias(text):
        return "c:" + text
    if latin_casefold(text):
        return "f:" + text.casefold()
    return "s:" + text


def iter_alias_specs(company: Mapping) -> list[tuple[str, bool]]:
    raws: list[str] = []
    for field in ("name_zh", "name_en", "ticker"):
        value = company.get(field)
        if value:
            raws.append(str(value))
    extra = company.get("aliases") or ()
    raws.extend(str(item) for item in extra)
    specs: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for raw in raws:
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if is_cjk_alias(text):
            if len(text) < 2:
                raise ValueError(f"中文别名至少两个字：{text}")
            specs.append((text, False))
        else:
            if len(text) < 2:
                raise ValueError(f"英文别名至少两个字符：{text}")
            specs.append((text, latin_casefold(text)))
    return specs


def _build_aho(patterns: list[tuple[str, int]]) -> _Node:
    root = _Node()
    for text, company_id in patterns:
        node = root
        for char in text:
            node = node.next.setdefault(char, _Node())
        node.out.append((company_id, len(text)))
    queue: deque[_Node] = deque()
    for child in root.next.values():
        child.fail = root
        queue.append(child)
    while queue:
        current = queue.popleft()
        for char, nxt in current.next.items():
            queue.append(nxt)
            fail = current.fail
            while fail is not None and char not in fail.next:
                fail = fail.fail
            nxt.fail = root if fail is None else fail.next.get(char, root)
            if nxt.fail is not None and nxt.fail.out:
                nxt.out = nxt.out + nxt.fail.out
    return root


def _search_aho(text: str, root: _Node) -> list[tuple[int, int, int]]:
    node = root
    raw: list[tuple[int, int, int]] = []
    for index, char in enumerate(text):
        while node is not root and char not in node.next:
            node = node.fail or root
        node = node.next.get(char, root)
        if node.out:
            company_id, length = max(node.out, key=lambda item: item[1])
            raw.append((index + 1 - length, index + 1, company_id))
    return _take_longest(raw)


def _take_longest(spans: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    chosen: list[tuple[int, int, int]] = []
    last_end = -1
    for start, end, company_id in spans:
        if start < last_end:
            continue
        chosen.append((start, end, company_id))
        last_end = end
    return chosen


def _compile_latin(items: list[tuple[str, int]], *, casefold: bool) -> tuple[re.Pattern[str] | None, dict[str, int]]:
    if not items:
        return None, {}
    ordered = sorted(items, key=lambda item: len(item[0]), reverse=True)
    lookup: dict[str, int] = {}
    parts: list[str] = []
    for text, company_id in ordered:
        key = text.casefold() if casefold else text
        lookup.setdefault(key, company_id)
        parts.append(re.escape(text))
    flags = re.IGNORECASE if casefold else 0
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])",
        flags,
    )
    return pattern, lookup


def company_rules(company: Mapping) -> dict:
    """公司表里的 alias_rules：{"needs": {别名: [佐证词]}, "exclude": [正则]}。没有就是空字典。"""
    raw = company.get("alias_rules") or {}
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _word_pattern(word: str) -> str:
    """佐证词：中文按字面；英文按词边界、区分大小写（「Mac」不会撞进「machine」）。"""
    escaped = re.escape(word)
    if is_cjk_alias(word):
        return escaped
    return r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])"


def compile_exclude(patterns: Iterable[str]) -> re.Pattern[str] | None:
    items = [str(item) for item in patterns if str(item).strip()]
    if not items:
        return None
    return re.compile("|".join(f"(?:{item})" for item in items))


def excluded_ranges(pattern: re.Pattern[str] | None, text: str) -> list[tuple[int, int]]:
    if pattern is None or not text:
        return []
    return [(match.start(), match.end()) for match in pattern.finditer(text) if match.end() > match.start()]


def inside(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    """命中整段落在排除写法里才不算：「烟台苹果」里的「苹果」不算，「很多字节跳动」里的「字节跳动」照算。"""
    return any(low <= start and end <= high for low, high in ranges)


@dataclass
class CompanyMatcher:
    companies: list[Mapping]

    def __post_init__(self) -> None:
        chinese: list[tuple[str, int]] = []
        folded: list[tuple[str, int]] = []
        sensitive: list[tuple[str, int]] = []
        self._exclude: dict[int, re.Pattern[str]] = {}
        # 公司 id → (要佐证的别名, 佐证词正则, {别名: 冲突词正则})
        self._needs: dict[int, tuple[frozenset[str], re.Pattern[str] | None, dict[str, re.Pattern[str]]]] = {}
        for company in self.companies:
            company_id = int(company["id"])
            specs = iter_alias_specs(company)
            for text, casefold in specs:
                if is_cjk_alias(text):
                    chinese.append((text, company_id))
                elif casefold:
                    folded.append((text, company_id))
                else:
                    sensitive.append((text, company_id))
            rules = company_rules(company)
            pattern = compile_exclude(rules.get("exclude") or [])
            if pattern is not None:
                self._exclude[company_id] = pattern
            needs = rules.get("needs") or {}
            if isinstance(needs, dict) and needs:
                guarded = frozenset(str(alias) for alias in needs)
                words = {str(word) for items in needs.values() for word in (items or []) if str(word).strip()}
                # 这家公司其他不受约束的写法（「Apple」「iPhone」「苹果公司」）本身也是佐证。
                words.update(text for text, _fold in specs if text not in guarded)
                ordered = sorted(words, key=len, reverse=True)
                corroborate = re.compile("|".join(_word_pattern(word) for word in ordered)) if ordered else None
                conflicts = {}
                for alias, items in (rules.get("conflicts") or {}).items():
                    terms = sorted({str(item) for item in items or [] if str(item).strip()}, key=len, reverse=True)
                    if terms:
                        conflicts[str(alias)] = re.compile("|".join(_word_pattern(term) for term in terms))
                self._needs[company_id] = (guarded, corroborate, conflicts)
        self._root = _build_aho(chinese)
        fold_re, fold_lookup = _compile_latin(folded, casefold=True)
        sens_re, sens_lookup = _compile_latin(sensitive, casefold=False)
        self._latin = (
            (fold_re, fold_lookup, True),
            (sens_re, sens_lookup, False),
        )

    def spans(self, text: str, *, blocked: frozenset = frozenset()) -> list[tuple[int, int, int]]:
        """blocked：这篇里没有佐证、不算数的 (公司 id, 别名)。由 document() 算出来。"""
        if not text:
            return []
        text = scrub_publisher_noise(text)
        found = _search_aho(text, self._root)
        latin: list[tuple[int, int, int]] = []
        for pattern, lookup, casefold in self._latin:
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                token = match.group(0)
                key = token.casefold() if casefold else token
                company_id = lookup.get(key)
                if company_id is None:
                    continue
                latin.append((match.start(), match.end(), company_id))
        found.extend(_take_longest(latin))
        if not (self._exclude or blocked):
            return found
        ranges: dict[int, list[tuple[int, int]]] = {}
        kept: list[tuple[int, int, int]] = []
        for start, end, company_id in found:
            if blocked and (company_id, text[start:end]) in blocked:
                continue
            pattern = self._exclude.get(company_id)
            if pattern is not None:
                if company_id not in ranges:
                    ranges[company_id] = excluded_ranges(pattern, text)
                if inside(ranges[company_id], start, end):
                    continue
            kept.append((start, end, company_id))
        return kept

    def count(self, text: str) -> dict[int, int]:
        counts: dict[int, int] = {}
        for _start, _end, company_id in self.spans(text):
            counts[company_id] = counts.get(company_id, 0) + 1
        return counts

    def document(self, *texts: str) -> DocumentMatcher:
        """一篇文章用的匹配器：「苹果」这类有歧义的别名，按全文判断一次算不算。

        有佐证词就算；没有佐证词时，给了冲突词的别名只在出现冲突词（说明可能在讲水果、粮食）时才不算，
        没给冲突词的别名一律不算。
        """
        if not self._needs:
            return DocumentMatcher(self, frozenset())
        full = scrub_publisher_noise("\n".join(text or "" for text in texts))
        blocked: set[tuple[int, str]] = set()
        for company_id, (guarded, corroborate, conflicts) in self._needs.items():
            if corroborate is not None and corroborate.search(full):
                continue
            for alias in guarded:
                conflict = conflicts.get(alias)
                if conflict is None or conflict.search(full):
                    blocked.add((company_id, alias))
        return DocumentMatcher(self, frozenset(blocked))


@dataclass(frozen=True)
class DocumentMatcher:
    """绑定了一篇文章的匹配器。没有佐证的歧义别名在这篇里一律不算。"""

    base: CompanyMatcher
    blocked: frozenset

    def spans(self, text: str) -> list[tuple[int, int, int]]:
        return self.base.spans(text, blocked=self.blocked)

    def count(self, text: str) -> dict[int, int]:
        counts: dict[int, int] = {}
        for _start, _end, company_id in self.spans(text):
            counts[company_id] = counts.get(company_id, 0) + 1
        return counts


def sentence_pieces(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.findall(text or "") if part.strip()]


def incidental_sentence(text: str) -> bool:
    return bool(_INCIDENTAL.search(text or ""))


def is_latin_text(text: str) -> bool:
    """正文主要是英文：字母够多，汉字不到字母数的四分之一。中文文章里夹几个英文名字不算。"""
    letters = len(_LATIN_LETTER.findall(text or ""))
    if letters < LATIN_MIN_LETTERS:
        return False
    return len(_CJK_CHAR.findall(text or "")) * 4 < letters


def latin_sentences(text: str) -> list[str]:
    """英文按句切。段落内的换行先接回去（Markdown 里单个换行就是空格，硬换行的稿子也一样切）。"""
    found: list[str] = []
    for block in _BLANK_LINE.split(text or ""):
        joined = " ".join(line.strip() for line in block.splitlines() if line.strip())
        start = 0
        for match in _LATIN_END.finditer(joined):
            words = joined[start : match.start()].split()
            last = words[-1].lower().strip("\"'“‘(") if words else ""
            following = joined[match.end() :].split(maxsplit=1)
            nxt = following[0] if following else ""
            # 缩写、单个字母的人名缩写（J. Smith）、后面接小写词的点（Inc. said）都不断句。
            # iPhone、eBay 这种小写开头、中间有大写的是名字，照样断。
            continues = nxt[:1].islower() and not any(char.isupper() for char in nxt[1:])
            if last in _NO_BREAK_AFTER or (len(last) == 1 and last.isalpha()) or continues:
                continue
            piece = joined[start : match.end()].strip()
            if piece:
                found.append(piece)
            start = match.end()
        tail = joined[start:].strip()
        if tail:
            found.append(tail)
    return found


def latin_incidental_sentence(text: str) -> bool:
    return bool(_LATIN_INCIDENTAL.search(text or "")) or incidental_sentence(text)


def substantive_text(text: str) -> str:
    return "\n".join(part for part in sentence_pieces(text) if not incidental_sentence(part))


def early_main_text(body: str) -> str:
    """正文开头几段，去掉类比和顺带一提的句子。卡片用它判断是不是主线。"""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", body or "") if block.strip()]
    if not blocks and (body or "").strip():
        blocks = [(body or "").strip()]
    chosen = blocks[:3]
    # 开头若只是「广告」这种标题残留，再多看两段，避免主线被标题行挤出窗口。
    if len(blocks) > 3 and chosen and all(len(block) <= 12 for block in chosen):
        chosen = blocks[:5]
    return substantive_text("\n".join(chosen))


def company_on_card(title: str, body: str, *, name: str, aliases: Iterable[str] = (), depth: str = "") -> bool:
    """卡片只留标题里的公司，或正文开头真正在讨论的公司。一带而过、后文顺带提到的不上卡片。"""
    if depth == DEPTH_TITLE:
        return True
    if depth == DEPTH_PASSING:
        return False
    names = [name, *aliases]
    names = [item.strip() for item in names if isinstance(item, str) and len(item.strip()) >= 2]
    if not names:
        return False
    title = scrub_publisher_noise(title or "")
    if any(item in title for item in names):
        return True
    early = scrub_publisher_noise(early_main_text(body))
    if not any(item in early for item in names):
        return False
    sentences = sentence_pieces(early)
    hits = [sentence for sentence in sentences if any(item in sentence for item in names)]
    if not hits:
        return False
    # 开头已经写了好几句，公司只在其中一句短话里出现，当作顺带一提。
    if len(sentences) >= 3 and len(hits) == 1 and len(hits[0]) <= 48:
        return False
    return True


def classify_depth(title_hits: int, section_hits: Iterable[int], *, structured: bool) -> str | None:
    sections = [count for count in section_hits]
    body_hits = sum(sections)
    sections_hit = sum(1 for count in sections if count > 0)
    if title_hits + body_hits <= 0:
        return None
    if title_hits > 0:
        return DEPTH_TITLE
    if not structured:
        if body_hits >= UNSTRUCTURED_WHOLE_HITS:
            return DEPTH_WHOLE
        return DEPTH_PASSING
    if sections_hit >= 2 or body_hits >= STRUCTURED_SECTION_WHOLE_HITS:
        return DEPTH_WHOLE
    if sections_hit == 1 and body_hits >= 2:
        return DEPTH_SECTION
    return DEPTH_PASSING


def mentions_for_document(matcher: CompanyMatcher, title: str, body_md: str) -> list[tuple[int, str, int]]:
    _frontmatter, body = split_frontmatter(body_md)
    body = strip_duplicate_title(body, title)
    raw_sections, structured = split_markdown_sections(body)
    sections = [markdown_to_text(section) for section in raw_sections]
    matcher = matcher.document(title, "\n".join(sections))
    title_counts = matcher.count(title)
    raw_counts = [matcher.count(section) for section in sections]
    if is_latin_text("\n".join(sections)):
        return _latin_mentions(matcher, title_counts, raw_counts, raw_sections, sections, structured)
    substantive_counts = [matcher.count(substantive_text(section)) for section in sections]
    sentences = sentence_pieces("\n".join(sections))
    sentence_hits: dict[int, int] = {}
    for sentence in sentences:
        if incidental_sentence(sentence):
            continue
        for company_id in matcher.count(sentence):
            sentence_hits[company_id] = sentence_hits.get(company_id, 0) + 1
    company_ids = set(title_counts)
    for counts in raw_counts:
        company_ids.update(counts)
    found: list[tuple[int, str, int]] = []
    for company_id in company_ids:
        title_hits = title_counts.get(company_id, 0)
        raw_per = [counts.get(company_id, 0) for counts in raw_counts]
        substantive_per = [counts.get(company_id, 0) for counts in substantive_counts]
        raw_hits = sum(raw_per)
        if title_hits + raw_hits <= 0:
            continue
        incidental_only = title_hits == 0 and sum(substantive_per) == 0 and raw_hits > 0
        # 长文里只在一句里出现，即使这句话把名字重复了几遍，也不是全文都在谈。
        one_liner = (
            title_hits == 0
            and not incidental_only
            and len(sentences) >= 3
            and sentence_hits.get(company_id, 0) <= 1
        )
        if incidental_only or one_liner:
            depth = DEPTH_PASSING
        else:
            depth = classify_depth(title_hits, substantive_per, structured=structured)
        if depth is None:
            continue
        found.append((company_id, depth, title_hits + raw_hits))
    return found


def _latin_mentions(
    matcher: DocumentMatcher,
    title_counts: dict[int, int],
    raw_counts: list[dict[int, int]],
    raw_sections: list[str],
    sections: list[str],
    structured: bool,
) -> list[tuple[int, str, int]]:
    """英文文章的深度。规则见 docs/mentions.md「英文文章」。"""
    # 每一句：(所在章节, 这句里谈到的公司)。举例、类比的句子不算谈到。
    sentences: list[tuple[int, frozenset[int]]] = []
    in_heading: set[int] = set()
    for index, (raw, text) in enumerate(zip(raw_sections, sections, strict=True)):
        heading = _HEADING_LINE.match(raw)
        if structured and heading:
            in_heading.update(matcher.count(markdown_to_text(heading.group(1))))
        for sentence in latin_sentences(text):
            named = frozenset() if latin_incidental_sentence(sentence) else frozenset(matcher.count(sentence))
            sentences.append((index, named))
    total = len(sentences)
    need_whole = max(LATIN_WHOLE_MIN, math.ceil(LATIN_WHOLE_SHARE * total))
    company_ids = set(title_counts)
    for counts in raw_counts:
        company_ids.update(counts)
    sentence_hits = {
        company_id: sum(company_id in named for _section, named in sentences) for company_id in company_ids
    }
    most = max(sentence_hits.values(), default=0)
    lead: set[int] = set()
    for _section, named in sentences[:LATIN_LEAD]:
        lead.update(named)
    found: list[tuple[int, str, int]] = []
    for company_id in company_ids:
        title_hits = title_counts.get(company_id, 0)
        raw_hits = sum(counts.get(company_id, 0) for counts in raw_counts)
        if title_hits + raw_hits <= 0:
            continue
        flags = [company_id in named for _section, named in sentences]
        hits = sentence_hits[company_id]
        lead_subject = company_id in lead and hits >= LATIN_WHOLE_MIN and hits == most
        per_section: dict[int, int] = {}
        for (section, _named), flag in zip(sentences, flags, strict=True):
            if flag:
                per_section[section] = per_section.get(section, 0) + 1
        window = sum(flags[:LATIN_WINDOW])
        best_window = window
        for end in range(LATIN_WINDOW, total):
            window += flags[end] - flags[end - LATIN_WINDOW]
            best_window = max(best_window, window)
        one_section = (
            structured
            and len(sections) >= 3
            and hits > 0
            and max(per_section.values()) >= LATIN_ONE_SECTION_SHARE * hits
        )
        if title_hits > 0:
            depth = DEPTH_TITLE
        elif (hits >= need_whole or lead_subject) and not one_section:
            depth = DEPTH_WHOLE
        elif hits >= need_whole or best_window >= LATIN_WINDOW_HITS or company_id in in_heading:
            depth = DEPTH_SECTION
        else:
            depth = DEPTH_PASSING
        found.append((company_id, depth, title_hits + raw_hits))
    return found


def replace_mentions(conn, article_id: int, rows: list[tuple[int, str, int]]) -> None:
    conn.execute("DELETE FROM article_mentions WHERE article_id = %s", (article_id,))
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO article_mentions (article_id, company_id, depth, hit_count)
            VALUES (%s, %s, %s, %s)
            """,
            [(article_id, company_id, depth, hit_count) for company_id, depth, hit_count in rows],
        )


def load_company_rows(conn) -> list[dict]:
    return list(
        conn.execute(
            """
            SELECT id, ticker, name_zh, name_en, market, aliases, alias_rules
            FROM companies
            ORDER BY id
            """
        ).fetchall()
    )


FINGERPRINT_KEY = "mentions_fingerprint"


def companies_fingerprint(rows: list[dict]) -> str:
    """公司表里影响匹配的字段（名字、代码、简称、歧义规则）加上深度规则版本的指纹。变了就要重算提及。"""
    import hashlib
    import json

    payload = [
        [
            int(row["id"]),
            row["name_zh"],
            row["name_en"] or "",
            row["ticker"] or "",
            sorted(row["aliases"] or []),
            json.dumps(company_rules(row), ensure_ascii=False, sort_keys=True),
        ]
        for row in sorted(rows, key=lambda item: int(item["id"]))
    ]
    # 深度规则的版本也算进去：规则一改，启动时就重算。
    payload = [["rules", MENTION_RULES_VERSION], *payload]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def rebuild_all(conn) -> int:
    rows_for_matcher = load_company_rows(conn)
    matcher = CompanyMatcher(rows_for_matcher)
    articles = conn.execute("SELECT id FROM articles ORDER BY id").fetchall()
    for item in articles:
        # 一篇一篇取正文，几万篇也不必一次读进内存。
        article = conn.execute(
            "SELECT id, title, body_md, duplicate_of, source_id, raw_meta FROM articles WHERE id = %s",
            (item["id"],),
        ).fetchone()
        if article is None:
            continue
        # 重复的那一份不记提及，公司页只算主版本。段永平已经搁下的也不记。
        suppressed = False
        if article["source_id"] == "duanyongping-blog":
            from researchlib.empty_answer import is_suppressed

            suppressed = is_suppressed(article["raw_meta"])
        rows = (
            []
            if article["duplicate_of"] or suppressed
            else mentions_for_document(matcher, article["title"], article["body_md"])
        )
        if article["source_id"] == "duanyongping-blog" and rows:
            from researchlib.ingest.duanyongping.attach import refine_mentions

            rows = refine_mentions(matcher, article["title"], article["body_md"], rows, article["raw_meta"])
        replace_mentions(conn, article["id"], rows)
        conn.commit()
    conn.execute(
        """
        INSERT INTO app_state (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (FINGERPRINT_KEY, companies_fingerprint(rows_for_matcher)),
    )
    conn.commit()
    return len(articles)


def mentions_stale(conn) -> bool:
    """公司表或深度规则版本和上次重算时不一样（或者从没记过），就算过期。没有文章时不算。"""
    row = conn.execute("SELECT value FROM app_state WHERE key = %s", (FINGERPRINT_KEY,)).fetchone()
    current = companies_fingerprint(load_company_rows(conn))
    if row is not None and row["value"] == current:
        return False
    has_articles = conn.execute("SELECT EXISTS (SELECT 1 FROM articles) AS e").fetchone()["e"]
    if not has_articles:
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at) VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (FINGERPRINT_KEY, current),
        )
        conn.commit()
        return False
    return True


def rebuild_if_stale(settings) -> int:
    """应用启动后在后台跑：公司别名或深度规则变了才重算全部提及。返回重算的篇数，没变返回 0。"""
    import logging

    from researchlib.db import connect

    logger = logging.getLogger("researchlib.mentions")
    conn = connect(settings)
    try:
        if not mentions_stale(conn):
            return 0
        logger.warning("公司别名变了或提及规则改了，后台重算全部文章的公司提及")
        count = rebuild_all(conn)
        logger.warning("公司提及重算完成：%s 篇", count)
        return count
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按当前公司别名重算全部文章的提及深度")
    parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        count = rebuild_all(conn)
    finally:
        conn.close()
    print(f"已重算 {count} 篇文章的公司提及")
    return 0


if __name__ == "__main__":
    sys.exit(main())

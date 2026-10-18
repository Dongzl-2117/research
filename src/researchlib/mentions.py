"""公司别名匹配和提及深度。

深度规则见 docs/mentions.md。匹配不走分词：中文最长不重叠，英文按词边界。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from researchlib.textutil import section_plain_texts, split_frontmatter, strip_duplicate_title

DEPTH_TITLE = "title"
DEPTH_WHOLE = "whole"
DEPTH_SECTION = "section"
DEPTH_PASSING = "passing"
DEPTHS = (DEPTH_TITLE, DEPTH_WHOLE, DEPTH_SECTION, DEPTH_PASSING)

# 无结构正文里，达到这个次数视为整篇都在谈。
UNSTRUCTURED_WHOLE_HITS = 3
# 有章节时，同一章达到这个次数也视为全文，而不是「单节」。
STRUCTURED_SECTION_WHOLE_HITS = 6

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


@dataclass
class CompanyMatcher:
    companies: list[Mapping]

    def __post_init__(self) -> None:
        chinese: list[tuple[str, int]] = []
        folded: list[tuple[str, int]] = []
        sensitive: list[tuple[str, int]] = []
        for company in self.companies:
            company_id = int(company["id"])
            for text, casefold in iter_alias_specs(company):
                if is_cjk_alias(text):
                    chinese.append((text, company_id))
                elif casefold:
                    folded.append((text, company_id))
                else:
                    sensitive.append((text, company_id))
        self._root = _build_aho(chinese)
        fold_re, fold_lookup = _compile_latin(folded, casefold=True)
        sens_re, sens_lookup = _compile_latin(sensitive, casefold=False)
        self._latin = (
            (fold_re, fold_lookup, True),
            (sens_re, sens_lookup, False),
        )

    def spans(self, text: str) -> list[tuple[int, int, int]]:
        if not text:
            return []
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
        return found

    def count(self, text: str) -> dict[int, int]:
        counts: dict[int, int] = {}
        for _start, _end, company_id in self.spans(text):
            counts[company_id] = counts.get(company_id, 0) + 1
        return counts


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
    sections, structured = section_plain_texts(body)
    title_counts = matcher.count(title)
    section_counts = [matcher.count(section) for section in sections]
    company_ids = set(title_counts)
    for counts in section_counts:
        company_ids.update(counts)
    found: list[tuple[int, str, int]] = []
    for company_id in company_ids:
        title_hits = title_counts.get(company_id, 0)
        per_section = [counts.get(company_id, 0) for counts in section_counts]
        depth = classify_depth(title_hits, per_section, structured=structured)
        if depth is None:
            continue
        found.append((company_id, depth, title_hits + sum(per_section)))
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
            SELECT id, ticker, name_zh, name_en, market, aliases
            FROM companies
            ORDER BY id
            """
        ).fetchall()
    )


def rebuild_all(conn) -> int:
    matcher = CompanyMatcher(load_company_rows(conn))
    articles = conn.execute("SELECT id, title, body_md FROM articles ORDER BY id").fetchall()
    for article in articles:
        rows = mentions_for_document(matcher, article["title"], article["body_md"])
        replace_mentions(conn, article["id"], rows)
    conn.commit()
    return len(articles)


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

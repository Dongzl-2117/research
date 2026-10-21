"""段永平文章的公司挂靠：只留下论证落在该公司上的提及。"""

from __future__ import annotations

from researchlib.author_voice import is_guest
from researchlib.ingest.duanyongping.policy import (
    body_argues,
    sentence_role,
    title_is_only_companies,
)
from researchlib.mentions import CompanyMatcher, sentence_pieces
from researchlib.textutil import markdown_to_text, split_frontmatter


def _off_views(raw_meta: dict | None) -> bool:
    return str((raw_meta or {}).get("off_views") or "").lower() == "true"


def argumentative_ids(matcher: CompanyMatcher, title: str, body_md: str) -> set[int]:
    """正文或标题里，论证确实落在其上的公司 id。列举、致谢、闲聊不算。"""
    _front, body = split_frontmatter(body_md or "")
    plain = markdown_to_text(body)
    document = matcher.document(title or "", plain)
    found: set[int] = set()
    for sentence in sentence_pieces(f"{title or ''}\n{plain}"):
        spans = document.spans(sentence)
        if not spans:
            continue
        surfaces = [sentence[start:end] for start, end, _company_id in spans]
        if sentence_role(sentence, surfaces) != "real":
            continue
        found.update(company_id for _start, _end, company_id in spans)
    title_spans = document.spans(title or "")
    if title_spans:
        surfaces = [(title or "")[start:end] for start, end, _company_id in title_spans]
        if title_is_only_companies(title or "", surfaces) and body_argues(plain):
            found.update(company_id for _start, _end, company_id in title_spans)
    return found


def refine_mentions(
    matcher: CompanyMatcher,
    title: str,
    body_md: str,
    rows: list[tuple[int, str, int]],
    raw_meta: dict | None = None,
) -> list[tuple[int, str, int]]:
    """博友帖、明确不进观点的，以及没有论证的公司，都不挂。"""
    if is_guest(raw_meta) or _off_views(raw_meta):
        return []
    allowed = argumentative_ids(matcher, title, body_md)
    return [row for row in rows if row[0] in allowed]

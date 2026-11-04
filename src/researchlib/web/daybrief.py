"""把一天的文章按作者分组，每篇配一句导读。不调用模型。

导读有文章摘要就用摘要，否则用正文开头。这一天的总结只用问过这一天的回答，由页面另接。
不拿导读拼总结：拼出来的就是各位作者第一篇的导读，下面原样还有一遍。
"""

from __future__ import annotations

import re


def _flat(text: str, limit: int = 90) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def compose_day_brief(items: list[dict]) -> dict:
    """items 需要 title、author，以及 summary 或 excerpt。"""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        author = item.get("author") or "未署名"
        if author not in grouped:
            grouped[author] = []
            order.append(author)
        grouped[author].append(item)
    sections: list[dict] = []
    for author in order:
        group = grouped[author]
        rendered = [{**item, "blurb": _flat(item.get("summary") or item.get("excerpt") or "")} for item in group]
        sections.append({"author": author, "count": len(group), "items": rendered})
    companies: list[dict] = []
    seen: set[str] = set()
    for item in items:
        for tag in item.get("tags") or []:
            name = tag.get("name") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            companies.append({"id": tag.get("id"), "name": name})
    return {
        "sections": sections,
        "companies": companies[:8],
        "authors": len(order),
    }

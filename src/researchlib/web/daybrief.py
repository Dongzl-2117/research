"""把一天的文章收成日汇总。不调用模型。

有文章摘要就用摘要，否则用正文开头。已经问过这一天的回答由页面另接，不在这里现算。
"""

from __future__ import annotations

import re


def _flat(text: str, limit: int = 90) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def compose_day_brief(items: list[dict]) -> dict:
    """items 需要 title、author，以及 summary 或 excerpt。返回的 lead 在有文章时不为空。"""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        author = item.get("author") or "未署名"
        if author not in grouped:
            grouped[author] = []
            order.append(author)
        grouped[author].append(item)
    sections: list[dict] = []
    lead_bits: list[str] = []
    for author in order:
        group = grouped[author]
        rendered = []
        first_blurb = ""
        for item in group:
            blurb = _flat(item.get("summary") or item.get("excerpt") or "")
            rendered.append({**item, "blurb": blurb})
            if blurb and not first_blurb:
                first_blurb = blurb
        sections.append({"author": author, "count": len(group), "items": rendered})
        if first_blurb:
            lead_bits.append(f"{author}：{first_blurb}")
        else:
            titles = "、".join(str(item.get("title") or "未命名") for item in group[:3])
            lead_bits.append(f"{author}写了{len(group)}篇（{titles}）")
    lead = "。".join(lead_bits)
    if lead and not lead.endswith("。"):
        lead += "。"
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
        "lead": lead,
        "sections": sections,
        "companies": companies[:8],
        "authors": len(order),
    }

"""日期、分页和公司页的月度柱状图。"""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from researchlib.company_seed import MARKET_LABELS
from researchlib.mentions import DEPTH_LABELS, DEPTHS


def format_date(value: datetime | None, zone_name: str) -> str:
    if value is None:
        return "日期不详"
    zone = ZoneInfo(zone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(zone).strftime("%Y-%m-%d")


def page_window(total: int, page: int, size: int) -> tuple[int, int, int]:
    pages = max(1, (total + size - 1) // size)
    current = min(max(page, 1), pages)
    return current, pages, (current - 1) * size


def month_chart(rows: list[dict]) -> str:
    if not rows:
        return ""
    shown = rows[-24:]
    width = 680
    height = 168
    pad_left = 8
    pad_bottom = 32
    max_count = max(int(row["n"]) for row in shown) or 1
    slot = (width - pad_left - 8) / len(shown)
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="按月提及文章数">',
    ]
    for index, row in enumerate(shown):
        count = int(row["n"])
        bar_height = max(2, round((count / max_count) * (height - pad_bottom - 18)))
        bar_width = min(36, max(8, slot * 0.45))
        x = pad_left + index * slot + (slot - bar_width) / 2
        y = height - pad_bottom - bar_height
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height}" rx="2">'
            f"<title>{escape(row['ym'])}：{count}</title></rect>"
        )
        if len(shown) <= 16 or index % 2 == 0:
            label = escape(str(row["ym"])[2:])
            parts.append(
                f'<text x="{x + bar_width / 2:.1f}" y="{height - 10}" text-anchor="middle">{label}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def depth_chips(counts: dict[str, int]) -> list[dict]:
    return [
        {"key": depth, "label": DEPTH_LABELS[depth], "count": counts.get(depth, 0)}
        for depth in DEPTHS
    ]


def market_label(market: str) -> str:
    return MARKET_LABELS.get(market, market or "其他")

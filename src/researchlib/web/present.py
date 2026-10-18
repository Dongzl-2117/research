"""日期、分页和公司页的月度柱状图。"""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from researchlib.company_seed import MARKET_LABELS
from researchlib.mentions import DEPTH_LABELS, DEPTHS

# 与 AIHOT 列表头一致：桌面用「星期三」，不写成「周三」。
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def localize(value: datetime | None, zone_name: str) -> datetime | None:
    if value is None:
        return None
    zone = ZoneInfo(zone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(zone)


def format_date(value: datetime | None, zone_name: str) -> str:
    local = localize(value, zone_name)
    if local is None:
        return "日期不详"
    return local.strftime("%Y-%m-%d")


def format_clock(value: datetime | None, zone_name: str) -> str:
    local = localize(value, zone_name)
    if local is None:
        return ""
    return local.strftime("%H:%M")


def format_iso(value: datetime | None, zone_name: str) -> str:
    local = localize(value, zone_name)
    if local is None:
        return ""
    return local.isoformat()


def day_key(value: datetime | None, zone_name: str) -> str:
    local = localize(value, zone_name)
    if local is None:
        return ""
    return local.strftime("%Y-%m-%d")


def day_label(key: str) -> str:
    if len(key) != 10 or key[4] != "-" or key[7] != "-":
        return "日期不详"
    return f"{int(key[5:7])}月{int(key[8:10])}日"


def weekday_label(key: str) -> str:
    if len(key) != 10 or key[4] != "-" or key[7] != "-":
        return ""
    try:
        found = datetime(int(key[0:4]), int(key[5:7]), int(key[8:10]))
    except ValueError:
        return ""
    return _WEEKDAYS[found.weekday()]


def relative_label(value: datetime | None, zone_name: str, now: datetime | None = None) -> str:
    local = localize(value, zone_name)
    if local is None:
        return ""
    current = now or datetime.now(local.tzinfo)
    seconds = max(0, int((current - local).total_seconds()))
    if seconds < 60:
        return "刚刚"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = round(minutes / 60)
    if hours < 24:
        return f"{hours} 小时前"
    days = round(hours / 24)
    if days < 30:
        return f"{days} 天前"
    months = round(days / 30)
    if months < 12:
        return f"{months} 个月前"
    years = max(1, round(days / 365))
    return f"{years} 年前"


def group_days(articles: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for article in articles:
        key = article.get("day") or ""
        if not groups or groups[-1]["key"] != key:
            groups.append(
                {
                    "key": key or "undated",
                    "label": day_label(key),
                    "weekday": weekday_label(key),
                    "items": [],
                }
            )
        groups[-1]["items"].append(article)
    for group in groups:
        group["count"] = len(group["items"])
    return groups


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

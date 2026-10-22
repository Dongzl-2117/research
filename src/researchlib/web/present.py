"""日期、分页，以及公司页的月度图。"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from researchlib.company_seed import MARKET_LABELS
from researchlib.mentions import DEPTH_LABELS, DEPTHS
from researchlib.textutil import clock_known_in, parse_datetime

# 与 AIHOT 列表头一致：桌面用「星期三」，不写成「周三」。
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
# 首页按天、公司页按月，都用上海日历。香港同一时刻落在同一天。
FEED_ZONE = "Asia/Shanghai"


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


def _is_midnight(value: datetime | None) -> bool:
    return value is not None and value.hour == 0 and value.minute == 0 and value.second == 0


def _known_flag(value) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def resolve_feed_instant(
    published_at: datetime | None,
    *,
    clock_hint=None,
    date_source: str = "",
    time_known=None,
    zone: str,
) -> tuple[datetime | None, bool]:
    """卡片上用的时刻，以及这个时刻是不是原文里真有的时分。

    published_at 被收成当地 00:00、但 raw_meta.create_time 同一天带了钟点时，用后者。
    只有日历日、没有更细的钟点时，第二项为 False，页面不显示 00:00。
    """
    hint = parse_datetime(clock_hint) if clock_hint not in (None, "") else None
    hint_known = clock_known_in(clock_hint) if clock_hint not in (None, "") else False
    local = localize(published_at, zone)
    hint_local = localize(hint, zone)
    if (
        hint_known
        and local is not None
        and hint_local is not None
        and _is_midnight(local)
        and not _is_midnight(hint_local)
        and local.date() == hint_local.date()
    ):
        return hint, True
    flag = _known_flag(time_known)
    if flag is True:
        return published_at, published_at is not None
    if flag is False:
        return published_at, False
    if published_at is None:
        if hint_known:
            return hint, True
        return None, False
    if not _is_midnight(local):
        return published_at, True
    if date_source == "meta":
        return published_at, True
    return published_at, False


def order_cards(cards: list[dict]) -> list[dict]:
    def key(card: dict) -> tuple:
        moment = card.get("sort_at")
        stamp = moment.timestamp() if isinstance(moment, datetime) else float("-inf")
        return (-stamp, -int(card.get("id") or 0))

    return sorted(cards, key=key)


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
                    "year": key[0:4] if len(key) == 10 else "",
                    "items": [],
                }
            )
        groups[-1]["items"].append(article)
    for group in groups:
        group["count"] = len(group["items"])
    return groups


def plan_feed_page(
    dated_days: int,
    undated: int,
    page: int,
    days_per_page: int,
    undated_page_size: int,
) -> dict:
    """按天翻页。有日期的一页只含完整的几天；没有日期的接在后面，按篇翻。"""
    days_per_page = max(1, days_per_page)
    undated_page_size = max(1, undated_page_size)
    day_pages = (dated_days + days_per_page - 1) // days_per_page if dated_days else 0
    undated_pages = (undated + undated_page_size - 1) // undated_page_size if undated else 0
    pages = max(1, day_pages + undated_pages)
    current = min(max(page, 1), pages)
    if day_pages and current <= day_pages:
        return {
            "page": current,
            "pages": pages,
            "mode": "days",
            "offset": (current - 1) * days_per_page,
            "limit": days_per_page,
        }
    undated_index = max(0, current - day_pages - 1)
    return {
        "page": current,
        "pages": pages,
        "mode": "undated" if undated else "days",
        "offset": undated_index * undated_page_size,
        "limit": undated_page_size,
    }


def page_window(total: int, page: int, size: int) -> tuple[int, int, int]:
    pages = max(1, (total + size - 1) // size)
    current = min(max(page, 1), pages)
    return current, pages, (current - 1) * size


def depth_chips(counts: dict[str, int]) -> list[dict]:
    return [
        {"key": depth, "label": DEPTH_LABELS[depth], "count": counts.get(depth, 0)}
        for depth in DEPTHS
    ]


def market_label(market: str) -> str:
    return MARKET_LABELS.get(market, market or "其他")


# —— 公司页和持仓卡片的月度图 ——


def _next_month(ym: str) -> str:
    year, month = int(ym[:4]), int(ym[5:7])
    month += 1
    if month > 12:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def month_series(rows: list[dict], *, first: str | None = None, last: str | None = None, limit: int = 120):
    """补齐中间没有提及的月份，返回连续的 [{ym, deep, passing}]。最长 limit 个月，多了只留最近的。"""
    by_month = {str(row["ym"]): row for row in rows}
    if not by_month and not (first and last):
        return []
    start = first or min(by_month)
    end = last or max(by_month)
    if start > end:
        start, end = end, start
    series = []
    current = start
    while current <= end and len(series) < 1200:
        row = by_month.get(current) or {}
        series.append({"ym": current, "deep": int(row.get("deep") or 0), "passing": int(row.get("passing") or 0)})
        current = _next_month(current)
    return series[-limit:]


def current_month(zone_name: str = FEED_ZONE) -> str:
    return datetime.now(ZoneInfo(zone_name)).strftime("%Y-%m")


def months_back(ym: str, count: int) -> str:
    year, month = int(ym[:4]), int(ym[5:7])
    total = year * 12 + (month - 1) - count
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def company_chart(series: list[dict]) -> str:
    """连续月份的堆叠柱：下面是深入（标题、全文、单节），上面是一带而过。

    SVG 横向拉伸填满宽度，年份另用 HTML 标在图下（见 chart_ticks），手机上字不会跟着缩小。
    """
    if not series:
        return ""
    width = 720
    height = 150
    top = 6
    left = 2
    peak = max(item["deep"] + item["passing"] for item in series) or 1
    slot = (width - left * 2) / len(series)
    bar = max(2.0, min(18.0, slot * 0.7))
    plot = height - top - 1
    parts = [
        f'<svg class="chart company-chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="按月提及篇数：深入和一带而过">',
        f'<line class="axis" x1="0" y1="{height - 0.5}" x2="{width}" y2="{height - 0.5}"/>',
    ]
    base = height - 1
    for index, item in enumerate(series):
        x = left + index * slot + (slot - bar) / 2
        deep_h = round(item["deep"] / peak * plot, 1)
        passing_h = round(item["passing"] / peak * plot, 1)
        tip = f"{item['ym']}：深入 {item['deep']} 篇，一带而过 {item['passing']} 篇"
        parts.append(f"<g><title>{escape(tip)}</title>")
        if deep_h:
            parts.append(
                f'<rect class="bar-deep" x="{x:.1f}" y="{base - deep_h:.1f}" width="{bar:.1f}" height="{deep_h}"/>'
            )
        if passing_h:
            parts.append(
                f'<rect class="bar-passing" x="{x:.1f}" y="{base - deep_h - passing_h:.1f}" width="{bar:.1f}" '
                f'height="{passing_h}"/>'
            )
        if not deep_h and not passing_h:
            parts.append(f'<rect class="bar-empty" x="{x:.1f}" y="{base - 1}" width="{bar:.1f}" height="1"/>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def chart_ticks(series: list[dict]) -> list[dict]:
    """图下的年份：每年一月的位置（百分比）。第一个月不是一月时也标上，挨得太近的跳过。"""
    if not series:
        return []
    ticks: list[dict] = []
    count = len(series)
    for index, item in enumerate(series):
        if index != 0 and not item["ym"].endswith("-01"):
            continue
        left = round((index + 0.5) / count * 100, 2)
        if ticks and left - ticks[-1]["left"] < 7:
            if index == 0:
                continue
            ticks.pop()
        ticks.append({"label": item["ym"][:4], "left": left})
    return ticks


def sparkline(series: list[dict]) -> str:
    """持仓卡片上的小图：近两年每月深入谈到它的篇数。没有的月份画一条细底线。"""
    if not series:
        return ""
    width = 240
    height = 40
    peak = max(item["deep"] for item in series) or 1
    slot = width / len(series)
    bar = max(2.0, slot * 0.62)
    parts = [
        f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="近两年每月深入谈到的篇数">'
    ]
    for index, item in enumerate(series):
        x = index * slot + (slot - bar) / 2
        if item["deep"]:
            h = max(3.0, round(item["deep"] / peak * (height - 2), 1))
            parts.append(
                f'<rect class="bar-deep" x="{x:.1f}" y="{height - h:.1f}" width="{bar:.1f}" height="{h}" rx="1">'
                f"<title>{escape(item['ym'])}：{item['deep']} 篇</title></rect>"
            )
        else:
            parts.append(f'<rect class="bar-empty" x="{x:.1f}" y="{height - 1}" width="{bar:.1f}" height="1"/>')
    parts.append("</svg>")
    return "".join(parts)


def recent_blurb(view: dict | None, digest: dict | None) -> str:
    """最近观点展开后的一段话。用库里已有的导读，没有导读再用这条观点的判断。

    不在请求里调模型。没有可用文字就返回空串，调用方不要画一个空展开。
    """
    summary = str((digest or {}).get("summary") or "").strip()
    if summary:
        return summary
    if not view:
        return ""
    return str(view.get("judgment") or "").strip()


# 断更提示只看最近这几天确实做过定时导入的来源：定时抓取脚本（crawl-*.sh，origin=crawl）或 AIHOT。
# 只手动导归档的来源，或定时任务没装的时候，「很久没有新文章」是常态，提示了也是假警报。
SCHEDULED_ORIGINS = frozenset({"crawl", "aihot"})
FRESH_IMPORT = timedelta(days=3)


def stale_sources(
    sources,
    times: dict[str, list[datetime]],
    imports: dict[str, dict],
    zone_name: str,
    now: datetime | None = None,
) -> list[dict]:
    """断更的来源：定时导入在跑，但最近一篇离现在比平时的间隔长很多（三倍以上、至少一周）。

    imports：{来源: {"at": 最近一次导入时间, "origin": "crawl" | "aihot" | "archive"}}。
    最近不到 10 篇带日期的来源不判断。
    返回 [{"name", "idle_days", "typical", "imported"}]；定时导入的来源全都断更时只返回一条
    {"all": True, "count", "idle_days"}：多半是抓取本身出了问题（令牌过期、接口变了），不是各位作者同时停更。
    """
    moment = now or datetime.now(UTC)
    found = []
    watched = 0
    for source in sources:
        record = imports.get(source.id) or {}
        imported = record.get("at")
        if imported is not None and imported.tzinfo is None:
            imported = imported.replace(tzinfo=UTC)
        if record.get("origin") not in SCHEDULED_ORIGINS or imported is None or moment - imported > FRESH_IMPORT:
            continue
        dates = [value if value.tzinfo else value.replace(tzinfo=UTC) for value in times.get(source.id) or []]
        if len(dates) < 10:
            continue
        watched += 1
        gaps = [(newer - older).total_seconds() / 86400 for newer, older in zip(dates, dates[1:], strict=False)]
        typical = statistics.median(gaps) if gaps else 0.0
        idle = (moment - dates[0]).total_seconds() / 86400
        if idle <= max(7.0, 3 * typical):
            continue
        if typical < 1.5:
            usual = "平时差不多天天有"
        else:
            usual = f"平时 {round(typical)} 天左右一篇"
        found.append(
            {
                "name": source.name,
                "idle_days": int(idle),
                "typical": usual,
                "imported": f"{format_date(imported, zone_name)} {format_clock(imported, zone_name)}",
            }
        )
    if watched >= 2 and len(found) == watched:
        return [{"all": True, "count": watched, "idle_days": min(item["idle_days"] for item in found)}]
    return found

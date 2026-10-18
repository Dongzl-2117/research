from datetime import datetime
from zoneinfo import ZoneInfo

from researchlib.web.present import (
    day_key,
    day_label,
    format_clock,
    group_days,
    plan_feed_page,
    relative_label,
    weekday_label,
)


def test_day_groups_follow_shanghai_calendar():
    when = datetime(2024, 3, 12, 16, 5, tzinfo=ZoneInfo("UTC"))
    assert day_key(when, "Asia/Shanghai") == "2024-03-13"
    assert format_clock(when, "Asia/Shanghai") == "00:05"
    assert day_label("2024-03-13") == "3月13日"
    assert weekday_label("2024-03-13") == "星期三"
    groups = group_days(
        [
            {"day": "2024-03-13", "id": 1},
            {"day": "2024-03-13", "id": 2},
            {"day": "2024-03-12", "id": 3},
            {"day": "", "id": 4},
        ]
    )
    assert [group["label"] for group in groups] == ["3月13日", "3月12日", "日期不详"]
    assert groups[0]["count"] == 2
    assert groups[0]["weekday"] == "星期三"
    assert groups[0]["year"] == "2024"
    assert groups[2]["weekday"] == ""
    assert groups[2]["year"] == ""


def test_feed_pages_keep_a_day_intact():
    first = plan_feed_page(3, 0, 1, 1, 20)
    assert first == {"page": 1, "pages": 3, "mode": "days", "offset": 0, "limit": 1}
    second = plan_feed_page(3, 0, 2, 1, 20)
    assert second["offset"] == 1
    assert second["mode"] == "days"
    undated = plan_feed_page(2, 25, 3, 1, 20)
    assert undated["pages"] == 4
    assert undated["mode"] == "undated"
    assert undated["offset"] == 0
    empty = plan_feed_page(0, 0, 1, 14, 20)
    assert empty["pages"] == 1
    assert empty["mode"] == "days"


def test_relative_label_matches_feed_copy():
    zone = "Asia/Shanghai"
    now = datetime(2026, 1, 15, 12, 0, tzinfo=ZoneInfo(zone))
    assert relative_label(datetime(2026, 1, 15, 11, 59, 30, tzinfo=ZoneInfo(zone)), zone, now) == "刚刚"
    assert relative_label(datetime(2026, 1, 9, 12, 0, tzinfo=ZoneInfo(zone)), zone, now) == "6 天前"
    assert relative_label(None, zone, now) == ""

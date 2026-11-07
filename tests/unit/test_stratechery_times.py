from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

from researchlib.stratechery_times import clock_docs, day_ranges, fetch_posts, slug_of, title_key
from researchlib.textutil import SHANGHAI


def _row(article_id, external_id, title, day):
    return {
        "id": article_id,
        "external_id": external_id,
        "title": title,
        "published_at": datetime.fromisoformat(day).replace(tzinfo=SHANGHAI),
    }


def _post(slug, title, local, gmt):
    return {"slug": slug, "title": {"rendered": title}, "date": local, "date_gmt": gmt}


def test_slug_and_title_keys():
    assert slug_of("2025/widget-margins") == "widget-margins"
    assert slug_of("2025\\widget-margins.md") == "widget-margins"
    assert slug_of("widget-margins.markdown") == "widget-margins"
    # 官网标题带 HTML 实体和标签，库里是排好的字。
    assert title_key("Widget&#8217;s <strike>Gadget</strike> Event, The New Widget") == title_key(
        "Widget’s Gadget Event, The New Widget"
    )
    assert title_key("Widget Margins") != title_key("Widget Margin")


def test_clock_docs_match_by_slug_then_title_on_the_same_day():
    rows = [
        _row(1, "2025/widget-margins", "Widget Margins", "2025-09-22"),
        # 官网晚上 9 点半发的：上海是第二天早上，日历日还记归档的那天。
        _row(2, "2025/gadget-day", "Gadget Day", "2025-09-23"),
        # 外部 id 里没有 slug（邮件导出），按标题加日历日对。
        _row(3, "email/Sprocket Update.md", "Sprocket Update: Prices", "2025-09-24"),
        # slug 对上了，日期却不一样：不认，标题也没有同一天的，算没对上。
        _row(4, "2025/widget-recap", "Widget Recap", "2025-09-25"),
        # 同一天两篇同名（官网一篇带 -2 后缀）：只靠标题分不出来，不猜。
        _row(5, "email/Twin.md", "Twin Interview", "2025-09-26"),
        # 带 slug 的那一篇照样分得出来。
        _row(6, "2025/twin-interview-2", "Twin Interview", "2025-09-26"),
    ]
    posts = [
        _post("widget-margins", "Widget Margins", "2025-09-22T06:00:00", "2025-09-22T10:00:00"),
        _post("gadget-day", "Gadget Day", "2025-09-23T21:30:00", "2025-09-24T01:30:00"),
        _post("sprocket-update-prices", "Sprocket Update: Prices", "2025-09-24T07:15:00", "2025-09-24T11:15:00"),
        _post("widget-recap", "Widget Recap", "2025-09-18T06:00:00", "2025-09-18T10:00:00"),
        _post("twin-interview", "Twin Interview", "2025-09-26T06:00:00", "2025-09-26T10:00:00"),
        _post("twin-interview-2", "Twin Interview", "2025-09-26T06:05:00", "2025-09-26T10:05:00"),
        _post("no-time", "No Time", "2025-09-27T06:00:00", ""),
    ]
    docs, missing = clock_docs(rows, posts)
    found = {doc.external_id: doc for doc in docs}
    assert missing == [4, 5]
    assert set(found) == {"2025/widget-margins", "2025/gadget-day", "email/Sprocket Update.md", "2025/twin-interview-2"}
    widget = found["2025/widget-margins"]
    assert widget.published_at == datetime(2025, 9, 22, 10, 0, tzinfo=UTC)
    assert widget.raw_meta == {"date_source": "wordpress", "time_known": True, "calendar_day": "2025-09-22"}
    gadget = found["2025/gadget-day"]
    assert gadget.published_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2025-09-24 09:30"
    assert gadget.raw_meta["calendar_day"] == "2025-09-23"
    assert found["email/Sprocket Update.md"].published_at == datetime(2025, 9, 24, 11, 15, tzinfo=UTC)
    assert found["2025/twin-interview-2"].published_at == datetime(2025, 9, 26, 10, 5, tzinfo=UTC)


def test_day_ranges_merge_close_days_and_split_far_ones():
    days = [date(2025, 9, 22), date(2010, 4, 13), date(2025, 10, 15), date(2025, 9, 22), date(2025, 11, 30)]
    assert day_ranges(days) == [
        (date(2010, 4, 13), date(2010, 4, 13)),
        (date(2025, 9, 22), date(2025, 10, 15)),
        (date(2025, 11, 30), date(2025, 11, 30)),
    ]


def test_fetch_posts_walks_every_page_with_a_day_of_margin():
    calls = []

    def get(url):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        page = int(query["page"][0])
        return [{"slug": f"post-{page}", "date": "2025-09-22T06:00:00", "date_gmt": "2025-09-22T10:00:00"}], {
            "x-wp-totalpages": "3"
        }

    posts = fetch_posts(date(2025, 9, 22), date(2025, 9, 30), get=get, pause=0)
    assert [post["slug"] for post in posts] == ["post-1", "post-2", "post-3"]
    assert [query["page"][0] for query in calls] == ["1", "2", "3"]
    first = calls[0]
    assert first["after"] == ["2025-09-21T00:00:00"] and first["before"] == ["2025-10-02T00:00:00"]
    assert first["per_page"] == ["100"] and first["_fields"] == ["slug,date,date_gmt,title"]

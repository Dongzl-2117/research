from datetime import datetime
from zoneinfo import ZoneInfo

from researchlib.textutil import (
    SHANGHAI,
    date_from_filename,
    date_from_markup,
    parse_datetime,
    resolve_published_at,
)


def test_chinese_and_zulu_dates_land_in_shanghai():
    chinese = parse_datetime("2024年5月1日 21:05")
    assert chinese.tzinfo == SHANGHAI
    assert (chinese.year, chinese.month, chinese.day, chinese.hour, chinese.minute) == (2024, 5, 1, 21, 5)
    zulu = parse_datetime("2024-05-01T00:00:00Z").astimezone(ZoneInfo("Asia/Shanghai"))
    assert (zulu.month, zulu.day, zulu.hour) == (5, 1, 8)
    assert parse_datetime("2024.5.1").day == 1
    assert parse_datetime("not a date") is None
    assert parse_datetime(None) is None


def test_filename_date_can_sit_in_the_middle():
    found = date_from_filename("笔记_2024-07-07_补充.md")
    assert (found.year, found.month, found.day) == (2024, 7, 7)
    assert date_from_filename("plain.md") is None
    assert date_from_filename("2024-02-31.md") is None
    compact = date_from_filename("20240501-note.md")
    assert (compact.month, compact.day) == (5, 1)


def test_markup_and_frontmatter_priority():
    marked = date_from_markup('<em id="publish_time">2022年3月4日 08:09</em>')
    assert (marked.year, marked.month, marked.day, marked.hour, marked.minute) == (2022, 3, 4, 8, 9)
    when, source = resolve_published_at(
        frontmatter={"publish_time": "2024年5月1日 21:05"},
        filename="1999-01-01-old.md",
        text='<em id="publish_time">2022年3月4日</em>',
    )
    assert source == "frontmatter"
    assert when == datetime(2024, 5, 1, 21, 5, tzinfo=SHANGHAI)
    named, named_source = resolve_published_at(filename="archive/2023-12-01-note.md")
    assert named_source == "filename"
    assert (named.year, named.month, named.day) == (2023, 12, 1)

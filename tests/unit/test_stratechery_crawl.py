from datetime import UTC, datetime
from pathlib import Path

import pytest

from researchlib.ingest.parse import parse_archive
from researchlib.stratechery_crawl import (
    FeedError,
    Item,
    archive_markdown,
    crawl,
    parse_feed,
    published_value,
)

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Stratechery</title>
<item>
  <title>Apple Earnings, Memory Problems</title>
  <link>https://stratechery.com/2026/apple-earnings-memory-problems/</link>
  <pubDate>Tue, 03 Nov 2026 11:00:00 +0000</pubDate>
  <category>Daily Update</category>
  <content:encoded><![CDATA[<p>Good morning, <a href="https://example.com/a">link</a>.</p>
<h2>Apple Earnings</h2><p>Text <strong>bold</strong>.</p>]]></content:encoded>
</item>
<item>
  <title>Sharp Tech episode</title>
  <link>https://sharptech.fm/episode/1</link>
  <pubDate>Tue, 03 Nov 2026 12:00:00 +0000</pubDate>
  <content:encoded><![CDATA[<p>podcast</p>]]></content:encoded>
</item>
<item>
  <title>No body</title>
  <link>https://stratechery.com/2026/no-body/</link>
</item>
</channel></rss>"""


def test_parse_feed_keeps_only_articles_with_body():
    items, skipped = parse_feed(FEED)
    assert skipped == 2
    assert [item.external_id for item in items] == ["2026/apple-earnings-memory-problems"]
    assert items[0].published == datetime(2026, 11, 3, 11, tzinfo=UTC)


@pytest.mark.parametrize("data", [b"<html><body>login</body></html>", b"not xml <"])
def test_parse_feed_rejects_login_pages(data):
    with pytest.raises(FeedError):
        parse_feed(data)


def _item(published, year="2026"):
    return Item(
        title="T", url=f"https://stratechery.com/{year}/t/", year=year, slug="t", published=published, html="<p>x</p>"
    )


def test_published_value_falls_back_to_eastern_day_across_new_year():
    assert published_value(_item(datetime(2026, 11, 3, 11, tzinfo=UTC))) == "2026-11-03T11:00:00+00:00"
    # 上海已经是 2027 年、链接还是 2026：只写美东日历日
    assert published_value(_item(datetime(2026, 12, 31, 18, tzinfo=UTC))) == "2026-12-31"
    assert published_value(_item(None)) == ""


def test_crawl_writes_new_and_archive_parses_back(tmp_path: Path):
    items, _ = parse_feed(FEED)
    inbox = tmp_path / "inbox"
    report, written = crawl(items, {"2025/old"}, inbox=inbox, author="Ben Thompson")
    assert report.failed == [] and len(written) == 1
    assert written[0] == inbox / "2026" / "apple-earnings-memory-problems.md"
    text = written[0].read_text(encoding="utf-8")
    assert "- source: member-rss" in text and "[link](https://example.com/a)" in text

    docs, errors = parse_archive(inbox, tmp_path / "work", id_mode="path")[:2]
    assert errors == []
    (doc,) = docs
    assert doc.external_id == "2026/apple-earnings-memory-problems"
    assert doc.title == "Apple Earnings, Memory Problems"
    assert doc.published_at == datetime(2026, 11, 3, 11, tzinfo=UTC)

    again, written_again = crawl(items, {"2026/apple-earnings-memory-problems"}, inbox=inbox, author="Ben Thompson")
    assert again.known == 1 and written_again == []


def test_dry_run_writes_nothing(tmp_path: Path):
    items, _ = parse_feed(FEED)
    report, written = crawl(items, set(), inbox=tmp_path, author="Ben Thompson", dry_run=True)
    assert written == [] and len(report.written) == 1 and not any(tmp_path.iterdir())


def test_archive_markdown_escapes_quotes():
    item = _item(datetime(2026, 11, 3, 11, tzinfo=UTC))
    item.title = 'Say "Hi"'
    assert 'title: "Say \\"Hi\\""' in archive_markdown(item, "body", "Ben Thompson")

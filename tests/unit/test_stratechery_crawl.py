from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from researchlib import stratechery_crawl as rss
from researchlib.ingest.parse import parse_archive
from researchlib.stratechery_crawl import (
    FeedError,
    Item,
    archive_markdown,
    crawl,
    parse_feed,
    published_value,
    resolve_by_title,
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
  <title>Google Earnings, Facebook D&#233;j&#224; Vu, The Agent Play</title>
  <link>https://stratechery.com/</link>
  <pubDate>Mon, 02 Nov 2026 11:00:00 +0000</pubDate>
  <content:encoded><![CDATA[<p>Google body</p>]]></content:encoded>
</item>
<item>
  <title>No body</title>
  <link>https://stratechery.com/2026/no-body/</link>
</item>
</channel></rss>"""


def test_parse_feed_keeps_only_articles_with_body():
    items, skipped, unresolved = parse_feed(FEED)
    assert skipped == 2
    assert [item.external_id for item in items] == ["2026/apple-earnings-memory-problems"]
    assert items[0].published == datetime(2026, 11, 3, 11, tzinfo=UTC)
    # 11-18 实测：一部分条目 link 只写了首页，留给官网接口认
    assert [item.title for item in unresolved] == ["Google Earnings, Facebook Déjà Vu, The Agent Play"]


def _wp(posts):
    def get(url):
        return posts, {"x-wp-totalpages": "1"}

    return get


def test_resolve_by_title_matches_title_and_time():
    _, _, unresolved = parse_feed(FEED)
    posts = [
        {
            "slug": "other",
            "date": "2026-11-02T06:00:00",
            "date_gmt": "2026-11-02T11:00:00",
            "title": {"rendered": "Something Else"},
        },
        {
            "slug": "google-earnings-facebook-deja-vu-the-agent-play",
            "date": "2026-11-02T06:00:00",
            "date_gmt": "2026-11-02T11:00:00",
            "title": {"rendered": "Google Earnings, Facebook D&#233;j&#224; Vu, The Agent Play"},
        },
    ]
    found, problems = resolve_by_title(unresolved, get=_wp(posts))
    assert problems == []
    assert [item.external_id for item in found] == ["2026/google-earnings-facebook-deja-vu-the-agent-play"]
    assert found[0].url == "https://stratechery.com/2026/google-earnings-facebook-deja-vu-the-agent-play/"


def test_resolve_by_title_reports_missing_and_far_dates():
    _, _, unresolved = parse_feed(FEED)
    far = [
        {
            "slug": "g",
            "date": "2026-10-01T06:00:00",
            "date_gmt": "2026-10-01T11:00:00",
            "title": {"rendered": "Google Earnings, Facebook D&#233;j&#224; Vu, The Agent Play"},
        }
    ]
    found, problems = resolve_by_title(unresolved, get=_wp(far))
    assert found == [] and len(problems) == 1


def test_resolve_by_title_survives_api_errors():
    _, _, unresolved = parse_feed(FEED)

    def broken(url):
        raise OSError("down")

    found, problems = resolve_by_title(unresolved, get=broken)
    assert found == [] and "官网接口连不上" in problems[0]


@pytest.mark.parametrize("data", [b"<html><body>login</body></html>", b"not xml <"])
def test_parse_feed_rejects_login_pages(data):
    with pytest.raises(FeedError):
        parse_feed(data)


def _item(published, year="2026"):
    return Item(
        title="T", url=f"https://stratechery.com/{year}/t/", year=year, slug="t", published=published, html="<p>x</p>"
    )


def test_published_value_preserves_utc_across_new_year():
    assert published_value(_item(datetime(2026, 11, 3, 11, tzinfo=UTC))) == "2026-11-03T11:00:00+00:00"
    # 上海已经是 2027 年、链接还是 2026，保留 RSS 明确给出的钟点。
    assert published_value(_item(datetime(2026, 12, 31, 18, tzinfo=UTC))) == "2026-12-31T18:00:00+00:00"
    assert published_value(_item(None)) == ""


def test_crawl_writes_new_and_archive_parses_back(tmp_path: Path):
    items, _, _ = parse_feed(FEED)
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
    items, _, _ = parse_feed(FEED)
    report, written = crawl(items, set(), inbox=tmp_path, author="Ben Thompson", dry_run=True)
    assert written == [] and len(report.written) == 1 and not any(tmp_path.iterdir())


def test_archive_markdown_escapes_quotes():
    item = _item(datetime(2026, 11, 3, 11, tzinfo=UTC))
    item.title = 'Say "Hi"'
    assert 'title: "Say \\"Hi\\""' in archive_markdown(item, "body", "Ben Thompson")


def test_resolve_by_title_accepts_weekly_number_prefix():
    item = Item(
        title="The End of Jensen",
        url="",
        year="",
        slug="",
        published=datetime(2026, 11, 13, 18, tzinfo=UTC),
        html="<p>x</p>",
    )
    posts = [
        {
            "slug": "the-end-of-jensen",
            "date": "2026-11-13T13:00:00",
            "date_gmt": "2026-11-13T18:00:00",
            "title": {"rendered": "2026.46: The End of Jensen"},
        }
    ]
    found, problems = resolve_by_title([item], get=_wp(posts))
    assert problems == [] and found[0].external_id == "2026/the-end-of-jensen"


@pytest.mark.parametrize("link", ["https://stratechery.com/podcasts/market-note/", "/podcasts/market-note/"])
def test_program_page_with_article_title_is_not_matched_to_an_article(link):
    data = FEED.replace(b"<link>https://stratechery.com/</link>", f"<link>{link}</link>".encode())
    _, skipped, unresolved = parse_feed(data)
    assert unresolved == [] and skipped == 3


def test_one_unusable_copy_does_not_hide_later_full_article(tmp_path):
    short = _item(datetime(2026, 11, 3, 11, tzinfo=UTC))
    short.html = "<!-- no article body -->"
    full = _item(short.published)
    full.html = "<p>The forecast is unchanged.</p>"
    report, paths = crawl([short, full], set(), inbox=tmp_path, author="Author")
    assert len(paths) == 1 and "forecast" in paths[0].read_text()


def test_verified_rss_clock_round_trips_when_shanghai_has_already_crossed_new_year(tmp_path):
    moment = datetime(2026, 12, 31, 18, tzinfo=UTC)
    item = _item(moment)
    report, _ = crawl([item], set(), inbox=tmp_path / "rss", author="Author")
    assert not report.failed
    docs, errors, *_ = parse_archive(tmp_path / "rss", tmp_path / "staging", id_mode="path")
    assert not errors
    assert docs[0].published_at == moment and docs[0].raw_meta["time_known"] is True


def test_homepage_missing_clock_reports_article_and_keeps_resolving_other_entries():
    items, _, unresolved = parse_feed(FEED)
    item = _item(None)
    item.title = "No clock"
    unresolved.append(item)
    found, issues = resolve_by_title(unresolved, get=_wp([]))
    assert len(items) == 1 and found == []
    assert any("No clock" in issue and "发布时间" in issue for issue in issues)
    assert any("Google" in issue for issue in issues)


def test_ambiguous_titles_and_exactly_one_day_apart_do_not_identify_original():
    _, _, unresolved = parse_feed(FEED)
    candidate = {"slug": "one", "date": "2026-11-02T06:00", "date_gmt": "2026-11-02T11:00:00",
                 "title": {"rendered": unresolved[0].title}}
    for entries in ([candidate, dict(candidate, slug="two")], [dict(candidate, date_gmt="2026-11-03T11:00:00")]):
        found, problems = resolve_by_title(unresolved, get=_wp(entries))
        assert not found and len(problems) == 1


def test_dry_run_honours_same_write_capacity_as_real_run(tmp_path):
    items, _, _ = parse_feed(FEED)
    for dry in [True, False]:
        report, paths = crawl(items, set(), inbox=tmp_path, author="Author", dry_run=dry, max_new=0)
        assert not paths and not report.written and len(report.failed) == 1
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(("body", "author", "known_short", "floor", "reads", "pending"), [
    ("preview", "Author", True, 5500, 0, False),
    ("界" * 2400, "Author", True, 5500, 1, False),
    ("preview", "A" * 6000, True, 5500, 1, False),
    ("preview", "Author", True, "file_bytes", 1, False),
    ("完整段落" * 1400, "Author", True, "body_chars", 1, True),
    ("preview", "Author", False, 5500, 1, True),
], ids=["short-preview", "multibyte-preview", "long-header", "exact-file-size", "sufficient-body", "new-archive"])
def test_pending_archives_only_skips_provably_short_previews(tmp_path, monkeypatch, body, author, known_short,
                                                            floor, reads, pending):
    item = _item(None)
    path = rss.archive_path(tmp_path, item)
    path.parent.mkdir()
    text = archive_markdown(item, body, author)
    path.write_text(text, encoding="utf-8")
    if floor == "file_bytes":
        floor = path.stat().st_size
    elif floor == "body_chars":
        floor = rss.body_chars(text)
    ids = {item.external_id} if known_short else set()
    read = Mock(wraps=rss.member_archive)
    monkeypatch.setattr(rss, "member_archive", read)
    problems = []
    result = rss.pending_archives(tmp_path, ids, ids, [], floor, 1, problems)
    assert result == ([path] if pending else []) and not problems
    assert read.call_count == reads


def test_pending_preview_disappears_after_directory_listing(tmp_path, monkeypatch):
    item = _item(None)
    path = rss.archive_path(tmp_path, item)
    path.parent.mkdir()
    path.write_text("preview")

    def vanished(directory, pattern):
        yield path
        path.unlink()

    monkeypatch.setattr(Path, "glob", vanished)
    ids = {item.external_id}
    problems = []
    assert rss.pending_archives(tmp_path, ids, ids, [], 5500, 1, problems) == []
    assert len(problems) == 1 and "FileNotFoundError" in problems[0]


def test_unreadable_small_preview_reports_a_failed_read(tmp_path, monkeypatch):
    item = _item(None)
    path = rss.archive_path(tmp_path, item)
    path.parent.mkdir()
    path.write_text("preview")
    monkeypatch.setattr(rss.os, "access", lambda path, mode: False)
    monkeypatch.setattr(rss, "member_archive", Mock(side_effect=PermissionError))
    ids = {item.external_id}
    problems = []
    assert rss.pending_archives(tmp_path, ids, ids, [], 5500, 1, problems) == []
    assert len(problems) == 1 and "PermissionError" in problems[0]


def test_archived_rss_keeps_only_unambiguous_in_page_links(tmp_path: Path):
    from dataclasses import replace

    from researchlib.mdlinks import prepare_body_html

    item = replace(_item(None), html=(
        '<p>Growth was 1%.<sup id="rf1-1768"><a href="#fn1-1768" rel="footnote">1</a></sup></p>'
        '<p><a href="#ai-impact">AI impact</a> · <a href="#generalist">Generalist</a></p>'
        '<a id="ai-impact"></a><h4>AI impact</h4><a name="generalist"></a><h4>Generalist</h4>'
        '<ol><li id="fn1-1768"><p>An actual note. <a href="#rf1-1768">↩</a></p></li></ol>'
        '<p>Plain 2, bare superscript <sup>3</sup>, <a href="#missing">missing</a>, '
        '<a href="#dup">ambiguous</a>, <a href="#hidden">hidden</a>.</p>'
        '<a id="dup"></a><a name="dup"></a><div style="display:none" id="hidden">ignored</div>'
    ))
    _, paths = crawl([item], set(), inbox=tmp_path, author="Ben Thompson")
    md = paths[0].read_text()
    html = prepare_body_html(md, item.title, {}, None, None)
    for anchor in ("rf1-1768", "fn1-1768", "ai-impact", "generalist"):
        assert html.count(f'id="archive-{anchor}"') == 1
        assert html.count(f'href="#archive-{anchor}"') == 1
    assert '<li><span id="archive-fn1-1768"></span>An actual note.' in html
    assert "Plain 2, bare superscript 3, missing, ambiguous, hidden." in html
    assert "#missing" not in md and "#dup" not in md and "#hidden" not in md

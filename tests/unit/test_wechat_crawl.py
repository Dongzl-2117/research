"""公众号定时抓取：原文页转 Markdown、极致了列表、跳过已有的、写归档。不连网，不连库。"""

import io
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from researchlib import wechat_crawl
from researchlib.sources import Source, load_sources, require_source
from researchlib.wechat_crawl import (
    Dajiala,
    DajialaError,
    Post,
    archive_markdown,
    crawl,
    parse_posts,
)
from researchlib.wechat_html import dajiala_markdown, page_markdown, unavailable_reason

SOURCE = Source(
    id="gudujinhuati", name="孤独进化体", author="孤独进化体", kind="markdown", enabled=True, ghid="gh_test"
)
SN_OLD = "a" * 32
SN_NEW = "b" * 32
SN_SAME_TITLE = "c" * 32

PAGE = """<html><head><meta property="og:title" content="新的一篇" /></head><body>
<div class="rich_media_content" id="js_content" style="visibility: hidden;">
<section><p><span>第一段，</span><span>接着写。</span></p>
<p><br></p>
<p style="text-align:center"><img class="rich_pages" data-src="https://mmbiz.qpic.cn/mmbiz_png/x1/640?wx_fmt=png&amp;from=appmsg"></p>
<p><strong>小标题</strong></p>
<p>前一行<br>后一行</p>
<section style="display:none"><p>看不见的字</p></section>
<script>var hidden = "脚本";</script>
</section>
</div>
<div id="js_pc_qr_code"><p>微信扫一扫</p></div>
</body></html>"""


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        rows = self.rows
        newest = max((row["published_at"] for row in rows if row.get("published_at")), default=None)

        class Result:
            def fetchall(self):
                return rows

            def fetchone(self):
                assert "max(published_at)" in sql
                return {"newest": newest}

        return Result()


class FakeClient:
    def __init__(self, posts, *, article=None, fail_article=False):
        self.posts = posts
        self.article_body = article
        self.fail_article = fail_article
        self.cost = 0.0
        self.history_calls = 0
        self.article_calls = []

    def history(self, ghid):
        self.history_calls += 1
        self.cost += 0.14
        return parse_posts(self.posts), {"remain_money": 9.5, "nickname": "孤独进化体"}

    def article(self, url):
        self.article_calls.append(url)
        self.cost += 0.03
        if self.fail_article:
            raise DajialaError("极致了 article_detail 返回 104：文章不存在")
        return {"code": 0, "title": "t", "content": self.article_body or ""}


def _post(sn, title, when):
    return {
        "url": f"http://mp.weixin.qq.com/s?__biz=MzX&mid=1&idx=1&sn={sn}&chksm=1#rd",
        "title": title,
        "post_time": int(when.timestamp()),
        "sn": sn,
        "digest": "摘要",
    }


def _known(sn="", title="", when=None):
    return {
        "title": title,
        "published_at": when,
        "head": f"# {title}\n\n- sn：`{sn}`\n" if sn else f"# {title}\n",
        "meta": "{}",
    }


def test_page_keeps_text_and_images_in_order():
    markdown, images = page_markdown(PAGE)
    assert markdown == (
        "第一段，接着写。\n\n"
        "![](https://mmbiz.qpic.cn/mmbiz_png/x1/640?wx_fmt=png&from=appmsg)\n\n"
        "**小标题**\n\n"
        "前一行\n\n后一行"
    )
    assert images == ["https://mmbiz.qpic.cn/mmbiz_png/x1/640?wx_fmt=png&from=appmsg"]


def test_page_without_content_says_why():
    assert page_markdown("<html><body><p>该内容已被发布者删除</p></body></html>") == ("", [])
    assert unavailable_reason("<p>该内容已被发布者删除</p>") == "原文已删除"
    assert unavailable_reason("<p>环境异常</p><p>完成验证后即可继续访问</p>") == "微信要求验证（环境异常）"
    assert unavailable_reason("<html></html>") == "原文页没有正文"


def test_dajiala_body_turns_literal_backslash_n_into_breaks():
    markdown, images = dajiala_markdown('<p>段一\\n\\n段二</p><p><img src="https://mmbiz.qpic.cn/y/0?wx_fmt=jpeg"></p>')
    assert markdown == "段一\n\n段二\n\n![](https://mmbiz.qpic.cn/y/0?wx_fmt=jpeg)"
    assert images == ["https://mmbiz.qpic.cn/y/0?wx_fmt=jpeg"]


def test_wechat_html_rejoins_leading_punctuation_and_split_year():
    """导入时就接上。图在句读前面时，句读回到上一段，字留在图后面。"""
    html = (
        "<p>吃的量就跟自己掏钱买的时候差不多了</p>"
        '<p><img src="https://mmbiz.qpic.cn/a.jpg">，巧克力是会腻的</p>'
        "<p>才算非常便宜。</p><p>1</p><p>988 年标普。</p>"
    )
    markdown, images = dajiala_markdown(html)
    assert images == ["https://mmbiz.qpic.cn/a.jpg"]
    assert markdown == (
        "吃的量就跟自己掏钱买的时候差不多了，\n\n"
        "![](https://mmbiz.qpic.cn/a.jpg)\n\n"
        "巧克力是会腻的\n\n"
        "才算非常便宜。\n\n"
        "1988 年标普。"
    )


def test_wechat_html_keeps_lone_section_numbers():
    """小节号「01」单独一段，入库时照原样留着；原文页 HTML 不存，这里删了就找不回来。"""
    markdown, _ = dajiala_markdown(
        "<p>引子说完了。</p><section><span>01</span></section><section><strong>先说生意模式</strong></section>"
    )
    assert markdown == "引子说完了。\n\n01\n\n**先说生意模式**"


def test_bold_split_across_paragraphs_is_dropped_not_left_dangling():
    markdown, _ = dajiala_markdown("<strong><p>一</p><p>二</p></strong><p>链接<a href='https://a.example/x'>原文</a></p>")
    assert markdown == "一\n\n二\n\n链接[原文](https://a.example/x)"


def test_posts_come_newest_first_with_sn_from_the_link():
    posts = parse_posts(
        [
            {"url": "http://mp.weixin.qq.com/s?sn=" + SN_OLD + "#rd", "title": "旧 &amp; 新", "post_time": 100},
            {"url": "https://mp.weixin.qq.com/s?sn=x", "title": "新", "post_time": 200, "sn": SN_NEW.upper()},
            {"url": "", "title": "没有链接", "post_time": 300},
        ]
    )
    assert [post.title for post in posts] == ["新", "旧 & 新"]
    assert posts[0].sn == SN_NEW
    assert posts[1].sn == SN_OLD
    assert posts[1].url == "https://mp.weixin.qq.com/s?sn=" + SN_OLD


def test_archive_header_matches_the_old_wechat_archives():
    post = Post(url="https://mp.weixin.qq.com/s?sn=" + SN_NEW, title="标题", post_time=1756342800, sn=SN_NEW, raw={})
    text = archive_markdown(post, "正文", "孤独进化体")
    assert text.startswith(
        "# 标题\n\n- 作者：孤独进化体\n- 时间：2025-08-28 09:00:00 (UTC+8)\n"
        f"- sn：`{SN_NEW}`\n- 原链接：https://mp.weixin.qq.com/s?sn={SN_NEW}\n"
        f"- 原始数据：[raw/articles/{SN_NEW}.json](../../raw/articles/{SN_NEW}.json)\n\n正文\n"
    )
    page = archive_markdown(post, "正文", "孤独进化体", images_from_page=True)
    assert page.endswith(f"(../../raw/articles/{SN_NEW}.json)\n- 图片位置：原文页\n\n正文\n")


def test_crawl_writes_only_new_articles(tmp_path):
    when = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
    client = FakeClient(
        [
            _post(SN_NEW, "新的一篇", when),
            _post(SN_OLD, "以前导入过", datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
            _post(SN_SAME_TITLE, "AIHOT 导过的", datetime(2026, 9, 1, 1, 0, tzinfo=UTC)),
        ]
    )
    conn = FakeConn(
        [
            _known(sn=SN_OLD, title="以前导入过", when=datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
            _known(title="AIHOT 导过的", when=datetime(2026, 9, 1, 3, 0, tzinfo=UTC)),
        ]
    )
    fetched = []

    def fetch(url):
        fetched.append(url)
        return PAGE

    report = crawl(conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=fetch)
    assert report.listed == 3 and report.known == 2
    assert fetched == [f"https://mp.weixin.qq.com/s?__biz=MzX&mid=1&idx=1&sn={SN_NEW}&chksm=1"]
    assert client.article_calls == []
    md = tmp_path / "md" / "2026" / f"2026-10-12-{SN_NEW}.md"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# 新的一篇\n\n- 作者：孤独进化体\n- 时间：2026-10-12 09:00:00 (UTC+8)\n")
    assert "第一段，接着写。" in text and "看不见的字" not in text and "微信扫一扫" not in text
    raw = json.loads((tmp_path / "raw" / "articles" / f"{SN_NEW}.json").read_text(encoding="utf-8"))
    assert raw["body_from"] == "page" and raw["post"]["sn"] == SN_NEW
    # 正文从原文页取：归档头写明图在原文的位置，阅读页不再按识图挪图。
    assert "\n- 图片位置：原文页\n\n第一段" in text
    rendered = report.render()
    assert "新写入归档: 1" in rendered and "原文页，1 张图" in rendered
    assert "花费 0.14 元，余额 9.50 元" in rendered
    assert report.failed == []

    # 再跑一次：归档里已经有了，不再取原文页。
    again = crawl(conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=fetch)
    assert again.known == 3 and again.written == [] and len(fetched) == 1


def test_crawl_falls_back_to_dajiala_when_the_page_has_no_body(tmp_path):
    client = FakeClient(
        [_post(SN_NEW, "图文消息", datetime(2026, 10, 12, 1, 0, tzinfo=UTC))],
        article="<p>极致了给的正文</p>",
    )
    report = crawl(
        FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=lambda url: "<p>环境异常</p>"
    )
    assert len(client.article_calls) == 1
    text = (tmp_path / "md" / "2026" / f"2026-10-12-{SN_NEW}.md").read_text(encoding="utf-8")
    assert text.rstrip().endswith("极致了给的正文")
    # 极致了的正文按段估图的位置：不写「图片位置：原文页」。
    assert "图片位置" not in text
    assert "极致了正文" in report.written[0]
    assert report.cost == pytest.approx(0.17)


def test_crawl_takes_nothing_when_the_list_is_another_account(tmp_path):
    """ghid 写成了别的号：列表里有比库里最新一篇还早的文章，却一篇都对不上，一篇都不取。"""
    client = FakeClient(
        [
            _post(SN_NEW, "别的号的新文章", datetime(2026, 10, 12, 1, 0, tzinfo=UTC)),
            _post(SN_SAME_TITLE, "别的号的旧文章", datetime(2026, 9, 1, 1, 0, tzinfo=UTC)),
        ]
    )
    conn = FakeConn([_known(sn=SN_OLD, title="库里最新的一篇", when=datetime(2026, 10, 10, 1, 0, tzinfo=UTC))])
    report = crawl(conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client,
                   fetch=lambda url: pytest.fail("不是这个号，不该取原文页"))
    assert report.written == [] and client.article_calls == []
    assert report.failed == [
        "列表里 2 篇，有比库里最新一篇还早的，却一篇都对不上库里的：多半不是这个号（ghid gh_test），这次一篇都不取"
    ]
    assert not tmp_path.joinpath("md").exists()
    assert "失败: 1" in report.render()
    dry = crawl(conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client,
                fetch=lambda url: pytest.fail("dry run 不取正文"), dry_run=True)
    assert dry.new == [] and "多半不是这个号" in dry.render()


def test_crawl_still_takes_posts_newer_than_the_library(tmp_path):
    """很久没抓、新文章超过一页：列表里都比库里新，一篇对不上也照常取。"""
    client = FakeClient(
        [
            _post(SN_NEW, "新的一篇", datetime(2026, 10, 12, 1, 0, tzinfo=UTC)),
            _post(SN_SAME_TITLE, "前一天的", datetime(2026, 10, 11, 1, 0, tzinfo=UTC)),
        ]
    )
    conn = FakeConn([_known(sn=SN_OLD, title="很久以前的", when=datetime(2026, 9, 1, 1, 0, tzinfo=UTC))])
    report = crawl(conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=lambda url: PAGE)
    assert report.failed == [] and len(report.written) == 2


def test_crawl_reports_a_failure_when_both_ways_fail(tmp_path):
    def broken(url):
        raise OSError("timed out")

    client = FakeClient([_post(SN_NEW, "取不到", datetime(2026, 10, 12, 1, 0, tzinfo=UTC))], fail_article=True)
    report = crawl(FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=broken)
    assert report.written == []
    assert report.failed == ["2026-10-12 取不到：原文页连不上：timed out；极致了 article_detail 返回 104：文章不存在"]
    assert not (tmp_path / "raw").exists()


def test_deleted_articles_are_recorded_and_not_fetched_again(tmp_path):
    client = FakeClient([_post(SN_NEW, "删掉了", datetime(2026, 10, 12, 1, 0, tzinfo=UTC))])
    deleted = "<html><body><p>该内容已被发布者删除</p></body></html>"
    report = crawl(FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=lambda url: deleted)
    assert report.gone == ["2026-10-12 删掉了：原文已删除"] and report.failed == []
    assert client.article_calls == []
    assert not (tmp_path / "md").exists()
    again = crawl(FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=lambda url: deleted)
    assert again.known == 1 and again.gone == []


def test_crawl_stops_at_max_new_and_dry_run_fetches_nothing(tmp_path):
    posts = [_post(f"{index:032x}", f"第{index}篇", datetime(2026, 10, index, 1, 0, tzinfo=UTC)) for index in (1, 2, 3)]
    report = crawl(FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=FakeClient(posts),
                   fetch=lambda url: PAGE, max_new=2)
    assert len(report.written) == 2 and report.later == 1
    assert [line.split(" ")[0] for line in report.written] == ["2026-10-03", "2026-10-02"]

    dry = crawl(FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path / "dry", client=FakeClient(posts),
                fetch=lambda url: pytest.fail("dry run 不取正文"), dry_run=True)
    assert dry.new == ["2026-10-03 第3篇", "2026-10-02 第2篇", "2026-10-01 第1篇"]
    assert not (tmp_path / "dry").exists()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_dajiala_request_and_errors_never_show_the_key(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        if request.full_url.endswith("post_history"):
            return _Response(json.dumps({"code": 0, "data": [], "cost_money": 0.14, "remain_money": 3}).encode())
        return _Response(json.dumps({"code": 20001, "msg": "余额不足", "cost_money": 0}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = Dajiala("SECRET-KEY", pause=0)
    posts, meta = client.history("gh_test")
    assert posts == [] and meta["remain_money"] == 3 and client.cost == pytest.approx(0.14)
    body = json.loads(seen[0].data)
    assert seen[0].get_method() == "POST" and body == {"ghid": "gh_test", "key": "SECRET-KEY", "verifycode": ""}
    with pytest.raises(DajialaError) as caught:
        client.article("https://mp.weixin.qq.com/s?sn=" + SN_NEW)
    assert "余额不足" in str(caught.value) and "SECRET-KEY" not in str(caught.value)
    assert "mode=1" in seen[1].full_url


def test_same_title_same_day_with_a_different_sn_is_still_fetched(tmp_path):
    """作者同一天发两篇同名文章（sn 不同）：库里那篇有 sn，第二篇照样取；标题加日期只给没 sn 的老文章兜底。"""
    when = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
    client = FakeClient([_post(SN_NEW, "同名文章", when)])
    conn = FakeConn([_known(sn=SN_OLD, title="同名文章", when=when)])
    report = crawl(conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=lambda url: PAGE)
    assert report.known == 0 and len(report.written) == 1


def test_empty_or_unusable_list_is_a_failure_not_no_news(tmp_path):
    for data in ([], [{"url": "", "title": ""}], [{"foo": 1}]):
        client = FakeClient(data)
        report = crawl(FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=lambda url: PAGE)
        assert report.listed == 0
        assert report.failed and "一篇能用的都没有" in report.failed[0]
        assert "失败: 1" in report.render()


class PagingClient:
    """按调用顺序交回一页。多翻就抛，用来守住 is_end 和 max-pages。"""

    def __init__(self, pages, nickname="孤独进化体"):
        self.pages = pages
        self.nickname = nickname
        self.calls = []
        self.cost = 0.0
        self.article_calls = []

    def history(self, ghid, offset=None):
        self.calls.append(offset or "")
        if len(self.calls) > len(self.pages):
            raise AssertionError(f"多翻了一页: {self.calls}")
        self.cost += 0.14
        raw, nxt, end = self.pages[len(self.calls) - 1]
        return parse_posts(raw), {
            "offset": nxt,
            "is_end": end,
            "remain_money": 8.5,
            "nickname": self.nickname,
        }

    def article(self, url):
        self.article_calls.append(url)
        self.cost += 0.03
        return {"content": "<p>极致了正文</p>", "title": "t"}


class BoomConn:
    def execute(self, sql, params=None):
        raise AssertionError(f"不该碰库: {sql}")


def _boom_fetch(url):
    raise AssertionError(f"不该取正文: {url}")


def test_daily_crawl_does_not_page_or_write_backfill_state(tmp_path):
    client = FakeClient([_post(SN_NEW, "新的一篇", datetime(2026, 10, 12, 1, 0, tzinfo=UTC))])
    report = crawl(FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=lambda url: PAGE)
    assert client.history_calls == 1 and report.backfill is False
    assert not (tmp_path / "raw" / "post-history-state.json").exists()
    assert not (tmp_path / "raw" / "post-history.jsonl").exists()


def test_daily_main_rejects_an_explicitly_different_account_before_fetch_or_import(tmp_path, monkeypatch, capsys):
    post = _post(SN_NEW, "另一公众号的文章", datetime(2026, 11, 18, tzinfo=UTC))
    client = PagingClient([([post], "", 1)], nickname="中国新闻网")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCES_FILE", str(Path(__file__).resolve().parents[2] / "config/sources.yaml"))
    monkeypatch.setenv("DAJIALA_KEY", "test-key")
    monkeypatch.setattr(wechat_crawl, "Dajiala", lambda *args: client)
    monkeypatch.setattr(wechat_crawl, "fetch_page", _boom_fetch)
    monkeypatch.setattr("researchlib.db.prepare", lambda settings: Mock(spec=["close"]))
    import_archive = Mock()
    monkeypatch.setattr("researchlib.ingest.cli.run_markdown_ingest", import_archive)
    inbox = tmp_path / "inbox" / "latepost"
    inbox.mkdir(parents=True)
    existing = inbox / "already-here.md"
    existing.write_text("已有归档")

    assert wechat_crawl.main(["--source", "latepost"]) == 1
    err = capsys.readouterr().err
    assert "中国新闻网" in err and "晚点LatePost" in err and "0.14 元" in err
    assert client.calls == [""] and client.article_calls == []
    assert list(inbox.rglob("*")) == [existing] and existing.read_text() == "已有归档"
    import_archive.assert_not_called()


@pytest.mark.parametrize(("source_id", "nickname"), [
    ("latepost", "晚点LatePost"), ("latepost", ""), ("fangwei-mp", "方伟看十年"), ("gudujinhuati", "孤独 进化体"),
])
def test_daily_matching_or_unreported_nickname_allows_body(tmp_path, source_id, nickname):
    sources = load_sources(Path(__file__).resolve().parents[2] / "config/sources.yaml")
    source = require_source(sources, source_id)
    post = _post(SN_NEW, "新的一篇", datetime(2026, 11, 18, tzinfo=UTC))
    client = PagingClient([([post], "", 1)], nickname=nickname)
    report = crawl(FakeConn([]), source, ghid=source.ghid, inbox=tmp_path, client=client, fetch=lambda url: PAGE)
    assert len(report.written) == 1 and not report.failed


def test_backfill_pages_by_offset_resumes_and_stops_at_is_end(tmp_path):
    sn_mid = "d" * 32
    pages = [
        ([_post(SN_NEW, "新", datetime(2026, 10, 3, 1, 0, tzinfo=UTC))], "cursor-1", 0),
        ([_post(SN_OLD, "中", datetime(2026, 10, 2, 1, 0, tzinfo=UTC))], "cursor-2", 0),
        ([_post(sn_mid, "旧", datetime(2026, 10, 1, 1, 0, tzinfo=UTC))], "", 1),
    ]
    client = PagingClient(pages)
    first = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True, max_pages=1,
    )
    assert client.calls == [""]
    assert first.list_calls == 1 and first.complete is False
    assert "累计列表调用 1 次" in first.page_logs[0]
    assert "估算 0.14 元" in first.page_logs[0]
    state = json.loads((tmp_path / "raw" / "post-history-state.json").read_text(encoding="utf-8"))
    assert state["next_offset"] == "cursor-1" and state["ghid"] == "gh_test"
    assert "key" not in state and state["is_end"] is False

    second = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True, max_pages=1,
    )
    assert client.calls == ["", "cursor-1"]
    assert "估算 0.28 元" in second.page_logs[-1]
    assert second.list_calls_total == 2

    third = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True,
    )
    assert client.calls == ["", "cursor-1", "cursor-2"]
    assert third.complete and third.listed == 3 and third.list_calls == 1
    rendered = third.render()
    assert "不写库" in rendered and "列表已经翻完" in rendered

    fourth = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True,
    )
    assert client.calls == ["", "cursor-1", "cursor-2"]
    assert fourth.list_calls == 0 and "不再向极致了查" in fourth.page_logs[0]


def test_backfill_stops_when_the_cursor_does_not_move_and_does_not_pay_again(tmp_path):
    pages = [([_post(SN_NEW, "新", datetime(2026, 10, 3, 1, 0, tzinfo=UTC))], "", 0)]
    client = PagingClient(pages)
    with pytest.raises(DajialaError, match="不会再自动扣费"):
        crawl(
            BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
            backfill=True, list_only=True,
        )
    assert client.calls == [""]
    assert SN_NEW in (tmp_path / "raw" / "post-history.jsonl").read_text(encoding="utf-8")
    with pytest.raises(DajialaError, match="不会再自动扣费"):
        crawl(
            BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
            backfill=True, list_only=True,
        )
    assert client.calls == [""]


def test_backfill_dedups_and_skips_non_articles_without_touching_the_db(tmp_path):
    video = _post(SN_NEW, "一条视频", datetime(2026, 10, 3, 1, 0, tzinfo=UTC))
    video["item_show_type"] = 5
    deleted = _post(SN_OLD, "已经删了", datetime(2026, 10, 2, 1, 0, tzinfo=UTC))
    deleted["is_deleted"] = "1"
    picture = _post(SN_SAME_TITLE, "一张图片", datetime(2026, 10, 2, 2, 0, tzinfo=UTC))
    picture["item_show_type"] = 8
    article = _post("d" * 32, "正文", datetime(2026, 10, 1, 1, 0, tzinfo=UTC))
    repeated = _post("d" * 32, "正文", datetime(2026, 10, 1, 1, 0, tzinfo=UTC))
    client = PagingClient([
        ([video, deleted, picture], "cursor-1", 0),
        ([repeated, article], "", 1),
    ])
    fetched = []

    def fetch(url):
        fetched.append(url)
        return PAGE

    report = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=fetch,
        backfill=True, list_only=True,
    )
    assert fetched == [] and client.article_calls == []
    assert report.listed == 4
    assert report.skip_counts["纯视频或纯音乐"] == 1
    assert report.skip_counts["已删除或违规"] == 1
    listing = (tmp_path / "raw" / "post-history.jsonl").read_text(encoding="utf-8")
    assert listing.count(f'"sn": "{"d" * 32}"') == 1

    again = crawl(
        FakeConn([]), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=fetch,
        backfill=True, max_pages=0,
    )
    assert client.calls == ["", "cursor-1"]
    assert len(fetched) == 2
    assert all("sn=" in url for url in fetched)
    assert {path.name for path in (tmp_path / "md").rglob("*.md")} == {
        f"2026-10-02-{SN_SAME_TITLE}.md",
        f"2026-10-01-{'d' * 32}.md",
    }
    assert again.known == 0 and len(again.written) == 2


def test_backfill_skips_articles_already_in_the_library(tmp_path):
    when = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
    client = PagingClient([([
        _post(SN_NEW, "新的一篇", when),
        _post(SN_OLD, "以前导入过", datetime(2026, 8, 28, 1, 0, tzinfo=UTC)),
    ], "", 1)])
    conn = FakeConn([_known(sn=SN_OLD, title="以前导入过", when=datetime(2026, 8, 28, 1, 0, tzinfo=UTC))])
    fetched = []
    report = crawl(
        conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client,
        fetch=lambda url: fetched.append(url) or PAGE, backfill=True,
    )
    assert len(fetched) == 1 and SN_NEW in fetched[0]
    assert report.known == 1 and len(report.written) == 1
    assert client.calls == [""]


def test_backfill_stops_at_since_and_can_continue_later(tmp_path):
    from datetime import date

    pages = [
        ([
            _post(SN_NEW, "六月", datetime(2024, 6, 1, 1, 0, tzinfo=UTC)),
            _post(SN_OLD, "一月中", datetime(2024, 1, 15, 1, 0, tzinfo=UTC)),
        ], "cursor-1", 0),
        ([
            _post(SN_SAME_TITLE, "元旦", datetime(2024, 1, 1, 1, 0, tzinfo=UTC)),
            _post("d" * 32, "上一年", datetime(2023, 12, 31, 1, 0, tzinfo=UTC)),
        ], "cursor-2", 0),
        ([_post("e" * 32, "更早", datetime(2023, 6, 1, 1, 0, tzinfo=UTC))], "", 1),
    ]
    client = PagingClient(pages)
    report = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True, since=date(2024, 1, 1),
    )
    assert client.calls == ["", "cursor-1"]
    assert report.complete is False and report.listed == 4
    state = json.loads((tmp_path / "raw" / "post-history-state.json").read_text(encoding="utf-8"))
    assert state["stopped_reason"] == "since" and state["next_offset"] == "cursor-2"

    held = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True, since=date(2024, 6, 1),
    )
    assert client.calls == ["", "cursor-1"]
    assert held.list_calls == 0

    rest = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True,
    )
    assert client.calls == ["", "cursor-1", "cursor-2"]
    assert rest.complete and rest.listed == 5


@pytest.mark.parametrize(("nickname", "accept"), [("方伟看十年", True), ("", True), ("中国新闻网", False)])
def test_history_uses_the_configured_wechat_name_for_fangwei(tmp_path, nickname, accept):
    sources = load_sources(Path(__file__).resolve().parents[2] / "config/sources.yaml")
    source = require_source(sources, "fangwei-mp")
    post = _post(SN_NEW, "方伟的一篇", datetime(2026, 11, 18, tzinfo=UTC))
    client = PagingClient([([post], "", 1)], nickname=nickname)
    report = crawl(BoomConn(), source, ghid=source.ghid, inbox=tmp_path, client=client,
                   fetch=_boom_fetch, backfill=True, list_only=True)
    assert report.listed == int(accept) and bool(report.failed) is not accept
    assert (tmp_path / "raw" / "post-history.jsonl").exists() is accept


def test_backfill_rejects_another_accounts_name_before_page_two(tmp_path):
    pages = [
        ([_post(SN_NEW, "别的号", datetime(2026, 10, 3, 1, 0, tzinfo=UTC))], "cursor-1", 0),
        ([_post(SN_OLD, "下一页", datetime(2026, 10, 2, 1, 0, tzinfo=UTC))], "", 1),
    ]
    client = PagingClient(pages, nickname="中国新闻网")
    report = crawl(
        BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
        backfill=True, list_only=True,
    )
    assert client.calls == [""]
    assert report.failed and "中国新闻网" in report.failed[0]
    assert not (tmp_path / "raw" / "post-history.jsonl").exists()


def test_backfill_does_not_keep_paging_a_wrong_ghid(tmp_path):
    pages = [
        ([
            _post(SN_NEW, "别的号的新文章", datetime(2026, 10, 12, 1, 0, tzinfo=UTC)),
            _post(SN_OLD, "别的号的旧文章", datetime(2026, 9, 1, 1, 0, tzinfo=UTC)),
        ], "cursor-1", 0),
        ([_post(SN_SAME_TITLE, "再往前", datetime(2026, 8, 1, 1, 0, tzinfo=UTC))], "", 1),
    ]
    client = PagingClient(pages)
    conn = FakeConn([_known(sn="f" * 32, title="库里最新的一篇", when=datetime(2026, 10, 10, 1, 0, tzinfo=UTC))])
    report = crawl(
        conn, SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch, backfill=True,
    )
    assert client.calls == [""] and client.article_calls == []
    assert report.written == [] and "一篇都不取" in report.failed[0]
    assert not (tmp_path / "raw" / "post-history.jsonl").exists()


def test_backfill_refuses_to_resume_a_different_ghid(tmp_path):
    state = tmp_path / "raw" / "post-history-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"ghid": "gh_other", "blocked": "", "is_end": False}), encoding="utf-8")
    client = PagingClient([([_post(SN_NEW, "新", datetime(2026, 10, 3, 1, 0, tzinfo=UTC))], "", 1)])
    with pytest.raises(DajialaError, match="另一个号"):
        crawl(
            BoomConn(), SOURCE, ghid="gh_test", inbox=tmp_path, client=client, fetch=_boom_fetch,
            backfill=True, list_only=True,
        )
    assert client.calls == []


def test_history_sends_offset_only_when_paging(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(json.loads(request.data))
        body = {"code": 0, "data": [], "cost_money": 0.14, "remain_money": 1, "offset": "CURSOR", "is_end": 0}
        if len(seen) == 2:
            body["is_end"] = "1"
            body["offset"] = ""
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = Dajiala("SECRET-KEY", pause=0)
    _posts, meta = client.history("gh_test")
    assert "offset" not in seen[0]
    assert seen[0]["key"] == "SECRET-KEY"
    assert meta["is_end"] is False and meta["offset"] == "CURSOR"
    _posts, meta = client.history("gh_test", "CURSOR")
    assert seen[1]["offset"] == "CURSOR" and seen[1]["ghid"] == "gh_test"
    assert meta["is_end"] is True
    assert "SECRET-KEY" not in json.dumps({key: value for key, value in meta.items()})


def test_list_only_main_does_not_open_the_database(monkeypatch, tmp_path, capsys):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCES_FILE", str(root / "config" / "sources.yaml"))
    monkeypatch.setenv("DAJIALA_KEY", "test-key")
    calls = []

    class Client:
        def __init__(self, key, base):
            assert key == "test-key"
            self.cost = 0.0

        def history(self, ghid, offset=None):
            assert ghid == "gh_44e9b2141610"
            calls.append(offset or "")
            self.cost += 0.14
            return parse_posts([_post(SN_NEW, "晚点的一篇", datetime(2026, 10, 12, 1, 0, tzinfo=UTC))]), {
                "offset": "",
                "is_end": 1,
                "remain_money": 3,
                "nickname": "晚点LatePost",
            }

        def article(self, url):
            raise AssertionError("不取正文")

    monkeypatch.setattr(wechat_crawl, "Dajiala", Client)
    monkeypatch.setattr(wechat_crawl, "fetch_page", _boom_fetch)
    import researchlib.db as db

    monkeypatch.setattr(db, "prepare", lambda settings: (_ for _ in ()).throw(AssertionError("不连库")))
    assert wechat_crawl.main(["--source", "latepost", "--list-only"]) == 0
    out = capsys.readouterr().out
    assert "不写库" in out and "累计列表调用: 1 次" in out
    listing = tmp_path / "inbox" / "latepost" / "raw" / "post-history.jsonl"
    assert SN_NEW in listing.read_text(encoding="utf-8")
    assert wechat_crawl.main(["--source", "latepost", "--list-only"]) == 0
    assert calls == [""]


def test_max_pages_without_backfill_is_refused(monkeypatch, tmp_path, capsys):
    from pathlib import Path

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCES_FILE", str(Path(__file__).resolve().parents[2] / "config" / "sources.yaml"))
    monkeypatch.setenv("DAJIALA_KEY", "test-key")
    assert wechat_crawl.main(["--source", "latepost", "--max-pages", "3"]) == 2
    assert "只查最新一页" in capsys.readouterr().err


def test_fetch_page_only_goes_to_wechat_article_pages(monkeypatch):
    from researchlib import wechat_crawl

    assert wechat_crawl.page_allowed("https://mp.weixin.qq.com/s?__biz=1&sn=" + SN_NEW)
    for url in (
        "https://127.0.0.1/s",
        "http://mp.weixin.qq.com/s",
        "https://mp.weixin.qq.com.evil.example/s",
        "https://evil.example/https://mp.weixin.qq.com/",
        "file:///etc/passwd",
    ):
        assert not wechat_crawl.page_allowed(url)
        with pytest.raises(ValueError):
            wechat_crawl.fetch_page(url)
    handler = wechat_crawl._PageRedirects()
    request = urllib.request.Request("https://mp.weixin.qq.com/s?x=1")
    with pytest.raises(ValueError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://10.0.0.1/admin")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://mp.weixin.qq.com/s?y=2") is not None


def test_crawl_recovers_body_missing_after_raw_record_was_written(tmp_path):
    item = _post(SN_NEW, "原文恢复", datetime(2026, 10, 12, 1, 0, tzinfo=UTC))
    raw = tmp_path / "raw" / "articles" / f"{SN_NEW}.json"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"body_from":"page"}', encoding="utf-8")
    client = FakeClient([item])
    actual = crawl(FakeConn([]), SOURCE, ghid="sample", inbox=tmp_path, client=client, fetch=lambda _: PAGE)
    assert len(actual.written) == 1 and not actual.failed
    assert list(tmp_path.glob("md/*/*.md"))
    again = crawl(FakeConn([]), SOURCE, ghid="sample", inbox=tmp_path, client=client, dry_run=True)
    assert again.known == 1 and not again.new


def _plant_archive(inbox, post, *, markdown=False, unavailable=""):
    raw = inbox / "raw" / "articles" / f"{post.sn}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    payload = {"post": {"sn": post.sn}}
    if unavailable:
        payload["unavailable"] = unavailable
    raw.write_text(json.dumps(payload), encoding="utf-8")
    if markdown:
        path = wechat_crawl.archive_path(inbox, post)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 已有正文\n", encoding="utf-8")


def test_backfill_and_daily_crawl_resume_the_same_interrupted_archive(tmp_path):
    """只有 raw、没有正文、也不是已删除：写到一半，回填和日常抓取都再取。正文在，或已删除，两边都跳过。"""
    partial = Post(
        url=f"https://mp.weixin.qq.com/s?sn={SN_NEW}", title="写到一半", post_time=1760227200, sn=SN_NEW, raw={}
    )
    done = Post(
        url=f"https://mp.weixin.qq.com/s?sn={SN_OLD}", title="已经写完", post_time=1756342800, sn=SN_OLD, raw={}
    )
    gone = Post(
        url=f"https://mp.weixin.qq.com/s?sn={SN_SAME_TITLE}",
        title="删掉了",
        post_time=1756688400,
        sn=SN_SAME_TITLE,
        raw={},
    )
    listed = [
        _post(post.sn, post.title, datetime.fromtimestamp(post.post_time, UTC))
        for post in (partial, done, gone)
    ]

    def seed(inbox):
        _plant_archive(inbox, partial)
        _plant_archive(inbox, done, markdown=True)
        _plant_archive(inbox, gone, unavailable="原文已删除")
        assert wechat_crawl.archived_post(inbox, partial) is False
        assert wechat_crawl.archived_post(inbox, done) is True
        assert wechat_crawl.archived_post(inbox, gone) is True

    def run(inbox, *, backfill):
        fetched = []
        client = (
            PagingClient([(listed, "", 1)])
            if backfill
            else FakeClient(listed)
        )
        report = crawl(
            FakeConn([]),
            SOURCE,
            ghid="gh_test",
            inbox=inbox,
            client=client,
            fetch=lambda url: fetched.append(url) or PAGE,
            backfill=backfill,
        )
        return report, fetched

    daily_inbox, backfill_inbox = tmp_path / "daily", tmp_path / "backfill"
    seed(daily_inbox)
    seed(backfill_inbox)
    daily, daily_fetched = run(daily_inbox, backfill=False)
    backfill, backfill_fetched = run(backfill_inbox, backfill=True)
    assert daily.known == backfill.known == 2
    assert len(daily.written) == len(backfill.written) == 1
    assert daily_fetched == backfill_fetched
    assert len(daily_fetched) == 1 and SN_NEW in daily_fetched[0]
    assert not daily.failed and not backfill.failed

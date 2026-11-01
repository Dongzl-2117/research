"""公众号定时抓取：原文页转 Markdown、极致了列表、跳过已有的、写归档。不连网，不连库。"""

import io
import json
import urllib.request
from datetime import UTC, datetime

import pytest

from researchlib.sources import Source
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

        class Result:
            def fetchall(self):
                return rows

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
    assert "极致了正文" in report.written[0]
    assert report.cost == pytest.approx(0.17)


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

"""一条坏输入不该卡死或中断整批（全栈审核组 5：I1、C6、I5、C1、C2、C4、B4、B8、T9）。

修之前：一串省略号加几个字让正则指数回溯几分钟；一个 NaN 时间值让整个来源导不进；一个写错的图片地址
让夜间下图每晚停在同一篇；模型交回 NUL 让识图整批中断；页码参数回 500；MCP 批量请求里一条坏参数
把别的结果一起丢掉；一个确定性的代码错误让同一篇每 12 小时整篇重译。
"""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from researchlib import boilerplate, present_text, remote_images, textutil, zsxq_title
from researchlib.ingest.parse import parse_archive
from researchlib.ocr_units import parse_reply, plain_units
from researchlib.textutil import parse_datetime
from researchlib.translate import service
from researchlib.translate.llm import LLMHTTPError, LLMTransportError
from researchlib.translate.numbers import same_numeric_facts
from researchlib.translate.provenance import AlignmentError
from researchlib.web import mcp
from researchlib.web.routes import MAX_PAGE, _page_number

FAST = 0.5  # 秒。修之前同样的输入要几秒到几小时。


def _timed(func, *args):
    started = time.monotonic()
    result = func(*args)
    assert time.monotonic() - started < FAST, f"{func.__name__} 太慢"
    return result


# I1 省略号正则


@pytest.mark.parametrize("dots", ["…" * 60, "。" * 80, "." * 100, "⋯" * 60, "… " * 400])
def test_long_ellipsis_runs_do_not_backtrack(dots):
    first = dots + "好吧，说正事"
    _timed(textutil.drop_export_stub, first + "\n好吧，说正事啊")
    _timed(textutil._same_truncated_heading, first, "好吧，说正事")
    _timed(zsxq_title.title_needs_repair, "我" + dots + "无语了")
    _timed(present_text._ELLIPSIS_TAIL.sub, "", "我" + dots + "无语了")


def test_ellipsis_rules_still_cut_the_same_places():
    assert textutil.drop_export_stub("IT之家 5 月 11 日消息...\nIT之家 5 月 11 日消息，正文") == (
        "IT之家 5 月 11 日消息，正文"
    )
    assert textutil._EDGE_ELLIPSIS.sub("", "……前面截掉了") == "前面截掉了"
    assert textutil._EDGE_ELLIPSIS.sub("", "后面截掉了……」 ...") == "后面截掉了"
    assert textutil._EDGE_ELLIPSIS.sub("", "他说……然后停了") == "他说……然后停了"
    assert textutil._EDGE_ELLIPSIS.sub("", "只有一个句号。") == "只有一个句号。"
    assert textutil._EDGE_ELLIPSIS.sub("", "两个点..") == "两个点.."
    assert zsxq_title._ELLIPSIS_END.sub("", "标题。。。) ") == "标题"
    assert zsxq_title._LEADING_CUT.sub("", "...后半句") == "后半句"
    assert present_text._ELLIPSIS_TAIL.sub("", "问的是..") == "问的是"


# C6 推销清洗的微信号正则


def test_contact_handle_regex_is_linear_on_long_blanks():
    _timed(boilerplate._HANDLE.search, "有问题请联系我" + "\xa0" * 3000 + "谢谢")
    _timed(boilerplate.strip_contact_spans, "（详见微信" + " \n" * 300 + "）")
    _timed(boilerplate.clean_markdown, "有问题请联系我" + "\xa0" * 3000 + "谢谢\n\n正文")


def test_contact_handle_regex_still_finds_handles():
    assert boilerplate._HANDLE.search("加我  微信   交流 :  abc_x9 谢谢")
    assert boilerplate.strip_contact_spans("狠狠地买入。（微信交流：abc123）后文") == "狠狠地买入。后文"
    assert not boilerplate._HANDLE.search("微信跟WhatsApp 都在用")


# I5 坏时间值


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), ".nan", 99999999999999999999, "99999999999999999999", True]
)
def test_parse_datetime_returns_none_for_broken_values(value):
    assert parse_datetime(value) is None


def test_parse_datetime_still_reads_good_values():
    assert parse_datetime(1700000000).year == 2023
    assert parse_datetime("2024-05-01 10:00").hour == 10


def test_one_bad_topic_does_not_stop_the_archive(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    for topic, stamp in (("111111", "NaN"), ("222222", '"2024-02-03T04:05:06.000+0800"')):
        (tmp_path / "md" / f"{topic}.md").write_text(f"星球正文 {topic}。\n", encoding="utf-8")
        (tmp_path / "raw" / f"{topic}.json").write_text(
            f'{{"topic_id": {topic}, "create_time": {stamp}, "title": "帖 {topic}"}}', encoding="utf-8"
        )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    by_id = {doc.external_id: doc for doc in docs}
    assert set(by_id) == {"111111", "222222"}
    assert by_id["222222"].published_at is not None
    # NaN 写不进 jsonb：存成字面。
    json.dumps(by_id["111111"].raw_meta, allow_nan=False)
    assert errors == []


def test_unexpected_parse_error_skips_only_that_file(tmp_path: Path, monkeypatch):
    from researchlib.ingest import parse

    (tmp_path / "md").mkdir()
    for name in ("good", "bad"):
        (tmp_path / "md" / f"{name}.md").write_text(f"# {name}\n\n正文 {name}。\n", encoding="utf-8")
    original = parse.derive_title

    def flaky(frontmatter, body, filename, *rest):
        if filename == "bad.md":
            raise RecursionError("坏文件")
        return original(frontmatter, body, filename, *rest)

    monkeypatch.setattr(parse, "derive_title", flaky)
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert [doc.title for doc in docs] == ["good"]
    assert len(errors) == 1 and "bad.md" in errors[0] and "RecursionError" in errors[0]


def test_wechat_crawl_huge_timestamp_is_unknown():
    from researchlib.wechat_crawl import Post, published

    assert published(Post(url="u", title="t", post_time=10**30, sn="s", raw={})) is None


# C1、C2 外链图片下载


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        remote_images.socket, "getaddrinfo", lambda host, *_a, **_k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )


@pytest.mark.parametrize(
    "url", ["http://[::1/x.png", "http://example.com:abc/x.png", "http://a..b/x.png", f"http://{'a' * 70}.com/x.png"]
)
def test_malformed_image_urls_become_fetch_errors(url, public_dns):
    with pytest.raises(remote_images.FetchError) as caught:
        remote_images.download(url)
    assert remote_images.failure_kind(str(caught.value)) in {"地址不对", "连不上", "域名解析不了"}


def test_other_exceptions_from_the_opener_become_fetch_errors(public_dns, monkeypatch):
    class _Opener:
        def open(self, request, timeout):
            raise http.client.InvalidURL("nonnumeric port")

    monkeypatch.setattr(remote_images, "_opener", _Opener)
    with pytest.raises(remote_images.FetchError, match="地址不对"):
        remote_images.download("http://example.com/x.png")


def test_odd_content_type_does_not_make_a_long_file_name():
    assert remote_images.image_extension(b"????", "image/" + "x" * 300) == ".img"
    assert remote_images.image_extension(b"????", "image/jpeg") == ".jpg"
    assert remote_images.image_extension(b"????", "image/svg+xml; charset=utf-8") == ".svg"
    assert remote_images.image_extension(b"????", "image/webp") == ".webp"


def test_trickling_server_hits_the_total_deadline(monkeypatch):
    clock = iter(range(0, 10_000, 7))
    monkeypatch.setattr(remote_images.time, "monotonic", lambda: next(clock))

    class _Trickle:
        def read1(self, _size):
            return b"x"

    with pytest.raises(remote_images.FetchError, match="下载超时"):
        remote_images._read_limited(_Trickle(), 0)


def test_read_limited_stops_past_the_size_cap(monkeypatch):
    monkeypatch.setattr(remote_images, "MAX_BYTES", 10)

    class _Big:
        def read1(self, size):
            return b"x" * 4

    assert len(remote_images._read_limited(_Big(), time.monotonic())) == 12


# C4 识图结果里的控制字符


def test_vision_reply_drops_nul_and_control_characters():
    units = parse_reply(
        '{"kind":"chat","blocks":[{"type":"text","text":"a\\u0000b\\u202ec\\td"},'
        '{"type":"message","speaker":"王\\u0000小明","text":"你好\\u0007"}]}'
    )
    assert units["blocks"][0]["text"] == "ab‮c\td".replace("‮", "")
    assert units["blocks"][1]["speaker"] == "王小明"
    assert units["blocks"][1]["text"] == "你好"
    assert plain_units("第一段\x00\n\n第二段")["blocks"] == [
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
    ]


# B4 页码


@pytest.mark.parametrize(
    ("raw", "page"),
    [
        (None, 1), ("", 1), ("²", 1), ("abc", 1), ("-3", 1), ("7", 7),
        ("99999999999999999999999", 1), ("50000", MAX_PAGE),
    ],
)
def test_page_number_never_raises(raw, page):
    assert _page_number(raw) == page


# B8 MCP


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 1.5, "abc"])
def test_mcp_bad_ids_are_tool_errors(value, monkeypatch):
    monkeypatch.setattr(mcp, "run_tool", lambda _request, _name, args: mcp._id(args))
    params = {"name": "get_article", "arguments": {"id": value}}
    reply = mcp.handle_message(None, {"id": 1, "method": "tools/call", "params": params})
    assert reply["result"]["isError"] is True


def test_mcp_huge_id_is_not_found():
    assert mcp._id({"id": 10**30}) == 0
    assert mcp._id({"id": "42"}) == 42


def test_mcp_missing_author_is_a_tool_error(monkeypatch):
    def missing(_request, _name, _args):
        raise HTTPException(status_code=404)

    monkeypatch.setattr(mcp, "run_tool", missing)
    params = {"name": "search_library", "arguments": {}}
    reply = mcp.handle_message(None, {"id": 7, "method": "tools/call", "params": params})
    assert reply["id"] == 7
    assert reply["result"]["isError"] is True


# T9 译制失败分类


def test_code_errors_are_rejections_not_request_failures():
    assert service._failure_kind(ValueError("Exceeds the limit (4300 digits)")) == ("rejected", "error:ValueError")
    assert service._failure_kind(AlignmentError("numbers"))[0] == "rejected"
    assert service._failure_kind(LLMTransportError("断线")) == ("failed", "request-failed")
    assert service._failure_kind(LLMHTTPError(402, "欠费")) == ("failed", "request-failed")
    assert service._failure_kind(LLMHTTPError(400, "内容被拒")) == ("failed", "request-failed")
    assert service._failure_kind(TimeoutError("超时")) == ("failed", "request-failed")


def test_errors_raised_in_the_request_layer_stay_request_failures():
    from researchlib.translate import llm

    namespace = {"__name__": "researchlib.translate.llm"}
    exec(compile("def boom():\n    raise RuntimeError('返回非 JSON')\n", llm.__file__, "exec"), namespace)
    try:
        namespace["boom"]()
    except RuntimeError as exc:
        assert service._failure_kind(exc) == ("failed", "request-failed")


def test_huge_digit_runs_do_not_raise_in_number_check():
    # 直接喂对齐那一步：从文字里抽 5000 位的数要好几秒（抽取对超长数字串是平方级，现实段落里碰不到）。
    from researchlib.translate.numbers import Fact, _align

    huge = Fact("literal", "1" * 5000)
    assert _align([huge], [huge]) is True
    assert same_numeric_facts("营收 3 亿", "营收 3 亿") is True

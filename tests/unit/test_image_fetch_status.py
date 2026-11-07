"""外链图片下不来时值班看得到：原因怎么分、什么时候提示、research-status 里那一句。不连库、不联网。"""

from __future__ import annotations

import socket
from collections import Counter

import pytest

from researchlib import remote_images
from researchlib.health import notices, read_status
from researchlib.remote_images import FetchError, download, failure_kind, fetch_status, record_fetch_status


def _report(new: int, kinds: dict | None = None, hosts: dict | None = None, retried: int = 0, retry_failed: int = 0):
    return {
        "new": new,
        "kinds": Counter(kinds or {}),
        "hosts": Counter(hosts or {}),
        "retried": retried,
        "retry_failed": retry_failed,
    }


def test_failure_kinds():
    assert failure_kind("HTTP 404") == "HTTP 404"
    assert failure_kind("HTTP 403") == "HTTP 403"
    assert failure_kind("地址解析到内网或本机，不下载") == "解析到内网"
    assert failure_kind("域名解析不了：[Errno -2] Name or service not known") == "域名解析不了"
    assert failure_kind("连不上：<urlopen error timed out>") == "连不上"
    assert failure_kind("不是图片（text/html）") == "不是图片"
    assert failure_kind("超过 15MB") == "超过 15MB"
    assert failure_kind("不是 http(s) 地址") == "地址不对"
    assert failure_kind("跳转到了 http(s) 以外的地址") == "地址不对"
    assert failure_kind("没见过的错") == "其他"
    # WordPress 的图两边都试过：回了状态码的那边说了算，都没有按原站。
    assert failure_kind("原站：HTTP 404；i0.wp.com：地址解析到内网或本机，不下载") == "HTTP 404"
    assert failure_kind("原站：连不上：timed out；i0.wp.com：HTTP 404") == "HTTP 404"
    assert failure_kind("原站：连不上：timed out；i0.wp.com：地址解析到内网或本机，不下载") == "连不上"


def test_dns_failure_is_not_recorded_as_a_private_address(monkeypatch):
    """以前解析不了也记成「地址解析到内网或本机」，和代理的 fake-ip 分不开。"""

    def no_dns(*_args, **_kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(remote_images.socket, "getaddrinfo", no_dns)
    with pytest.raises(FetchError, match="^域名解析不了"):
        download("https://gone.example/a.png")


def test_most_new_images_failing_is_reported_with_kinds_and_hosts():
    ok, message = fetch_status(_report(40, {"解析到内网": 30, "HTTP 404": 5}, {"i0.wp.com": 33, "example.com": 2}))
    assert not ok
    assert message == (
        "新地址 40 个，下下来 5 个，下不来 解析到内网 30、HTTP 404 5（最多的是 i0.wp.com 33、example.com 2）"
    )


def test_a_few_dead_links_are_not_a_problem():
    ok, message = fetch_status(_report(50, {"HTTP 404": 3}, {"example.com": 3}, retried=4, retry_failed=4))
    assert ok
    assert message.endswith("；重试以前下不来的 4 个，又失败 4 个")


def test_many_failures_are_a_problem_even_when_most_images_came_down():
    ok, _message = fetch_status(_report(300, {"HTTP 403": 20}, {"example.com": 20}))
    assert not ok


def test_too_few_new_addresses_leave_the_last_verdict():
    assert fetch_status(_report(0, retried=10, retry_failed=10)) is None
    assert fetch_status(_report(3, {"连不上": 3}, {"example.com": 3})) is None
    # 都下下来了就算没问题，哪怕只有两个：上一次的提示可以撤掉了。
    assert fetch_status(_report(2)) == (True, "新地址 2 个，下下来 2 个")


def test_status_file_and_the_research_status_line(tmp_path):
    message = record_fetch_status(tmp_path, _report(10, {"连不上": 8}, {"example.com": 8}))
    saved = read_status(tmp_path, "fetch-images")
    assert saved["ok"] is False and saved["new"] == 10
    assert saved["kinds"] == {"连不上": 8} and saved["hosts"] == {"example.com": 8}
    lines = notices(tmp_path, "Asia/Shanghai")
    assert len(lines) == 1
    assert lines[0].startswith("外链图片下载失败（") and lines[0].endswith("：" + message)
    # 只有重试：状态文件不动，提示还在。
    assert record_fetch_status(tmp_path, _report(0, retried=3, retry_failed=3)) == ""
    assert len(notices(tmp_path, "Asia/Shanghai")) == 1
    # 下一次新地址都下下来了：提示撤掉。
    record_fetch_status(tmp_path, _report(6))
    assert notices(tmp_path, "Asia/Shanghai") == []

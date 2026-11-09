"""外链图片下不来时值班看得到：原因怎么分、什么时候提示、research-status 里那一句。不连库、不联网。"""

from __future__ import annotations

import socket
from collections import Counter

import pytest

from researchlib import remote_images
from researchlib.health import notices, read_status
from researchlib.remote_images import (
    EXIT_MOSTLY_FAILED,
    FetchError,
    build_parser,
    download,
    failure_kind,
    fetch_status,
    finish,
    record_fetch_status,
)


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
    # 代理的假地址和真正的内网分开；错误后面带着「（域名 → 地址）」也照样认。
    assert failure_kind("地址解析到代理的假地址，不下载（evil.example → 198.18.0.9）") == "解析到代理假地址"
    assert failure_kind("地址解析到内网或本机，不下载（evil.example → 10.0.0.5）") == "解析到内网"
    assert failure_kind(
        "原站：地址解析到代理的假地址，不下载（a.example → 198.18.0.3）；i0.wp.com：HTTP 404"
    ) == "HTTP 404"
    assert failure_kind(
        "原站：地址解析到代理的假地址，不下载（a.example → 198.18.0.3）；i0.wp.com：连不上：timed out"
    ) == "解析到代理假地址"


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
    ok, message = record_fetch_status(tmp_path, _report(10, {"连不上": 8}, {"example.com": 8}))
    assert not ok
    saved = read_status(tmp_path, "fetch-images")
    assert saved["ok"] is False and saved["new"] == 10
    assert saved["kinds"] == {"连不上": 8} and saved["hosts"] == {"example.com": 8}
    lines = notices(tmp_path, "Asia/Shanghai")
    assert len(lines) == 1
    assert lines[0].startswith("外链图片下载失败（") and lines[0].endswith("：" + message)
    # 只有重试：状态文件不动，提示还在。
    assert record_fetch_status(tmp_path, _report(0, retried=3, retry_failed=3)) is None
    assert len(notices(tmp_path, "Asia/Shanghai")) == 1
    # 下一次新地址都下下来了：提示撤掉。
    record_fetch_status(tmp_path, _report(6))
    assert notices(tmp_path, "Asia/Shanghai") == []


def test_nightly_step_fails_when_most_new_images_fail(tmp_path, capsys):
    """11-09 夜里 300 个新地址一张没下下来，夜间补齐却报成功：以前不管下下来几张都是退出码 0。
    夜间补齐带 --exit-code，新地址大半下不来时退出码 3，那句话在错误输出最后一行，nightly.sh 记进失败原因。"""
    assert build_parser().parse_args(["--limit", "300", "--exit-code"]).exit_code
    assert not build_parser().parse_args(["--limit", "200"]).exit_code
    bad = _report(300, {"解析到代理假地址": 300}, {"i0.wp.com": 300})
    assert finish(tmp_path, bad, exit_code=True) == EXIT_MOSTLY_FAILED == 3
    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip().splitlines()[-1] == (
        "新地址 300 个，下下来 0 个，下不来 解析到代理假地址 300（最多的是 i0.wp.com 300）"
    )
    assert read_status(tmp_path, "fetch-images")["ok"] is False
    # 不带 --exit-code（定时抓取、手动跑）照旧退出码 0，那句话打在标准输出；状态文件照样写。
    assert finish(tmp_path, bad) == 0
    assert capsys.readouterr().out.startswith("新地址 300 个，下下来 0 个")
    # 个别下不来、只有重试、第一次下的太少：都不算这一步失败。
    assert finish(tmp_path, _report(50, {"HTTP 404": 3}, {"example.com": 3}), exit_code=True) == 0
    assert finish(tmp_path, _report(0, retried=9, retry_failed=9), exit_code=True) == 0
    assert finish(tmp_path, _report(3, {"连不上": 3}, {"example.com": 3}), exit_code=True) == 0

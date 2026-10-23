"""保住全文：改稿、删稿的判断和版本对比；外链图片的地址抽取、只连公网、下载后的显示；图片文字块。"""

import socket
import urllib.request

import pytest

from researchlib.chunks import OCR_MIN_CHARS, ocr_chunks
from researchlib.ocr import order_text
from researchlib.remote_images import (
    FetchError,
    _checked_create_connection,
    _Redirects,
    blocked_address,
    download,
    external_urls,
    image_extension,
)
from researchlib.render import image_anchor, img_tag_url, normalize_image_url, render_body
from researchlib.revisions import diff_blocks, judge_replacement, label
from researchlib.wechat_dialogue import BUBBLE_UNKNOWN, SIDE_SELF, present_ocr_pieces

LONG = "腾讯的广告加载率还在提升，视频号是新的广告库存。" * 30


def test_deleted_notice_and_heavy_shrink_are_held():
    assert judge_replacement(LONG, "该内容已被发布者删除") == "deleted"
    assert judge_replacement(LONG, "此内容因违规无法查看\n\n点击查看更多") == "deleted"
    assert judge_replacement(LONG, LONG[: len(LONG) // 3]) == "shrunk"
    # 正常改稿、加了几句、短帖之间的改动都照常替换。
    assert judge_replacement(LONG, LONG + "补充一句。") == ""
    assert judge_replacement(LONG, LONG[: int(len(LONG) * 0.8)]) == ""
    assert judge_replacement("短帖。", "改过的短帖。") == ""
    # 很长的正文里引用了「已被删除」的字样不算删稿。
    assert judge_replacement(LONG, LONG + "原文说该内容已被发布者删除，我们存了一份。") == ""


def test_diff_blocks_keep_context_and_mark_changes():
    old = "\n".join(f"第 {index} 段没变。" for index in range(8)) + "\n原来的结论：看好。"
    new = "\n".join(f"第 {index} 段没变。" for index in range(8)) + "\n改后的结论：看淡。"
    blocks = diff_blocks(old, new)
    kinds = [block["kind"] for block in blocks]
    assert kinds[-2:] == ["del", "add"]
    assert blocks[-2]["text"] == "原来的结论：看好。" and blocks[-1]["text"] == "改后的结论：看淡。"
    assert "skip" in kinds and kinds.count("same") <= 2


def test_revision_label_prefers_held_versions():
    assert label(None) == ""
    assert label({"previous": 2, "held": ""}) == "改过"
    assert label({"previous": 1, "held": "deleted"}) == "后来被删"
    assert label({"previous": 0, "held": "shrunk"}) == "后来被截短"


def test_external_urls_cover_markdown_html_and_lazy_images():
    body = (
        '![图](https://mmbiz.qpic.cn/a.png "标题")\n'
        '<p><img class="rich" data-src="https://mmbiz.qpic.cn/b.jpeg?wx_fmt=jpeg&amp;tp=webp" '
        'src="data:image/svg+xml,xx"></p>\n'
        "<img src='//img.example.com/c.gif'>\n"
        "![本地](images/d.png)\n"
        "![重复](https://mmbiz.qpic.cn/a.png)"
    )
    assert external_urls(body) == [
        "https://mmbiz.qpic.cn/a.png",
        "https://mmbiz.qpic.cn/b.jpeg?wx_fmt=jpeg&tp=webp",
        "https://img.example.com/c.gif",
    ]
    assert img_tag_url('<img src="x.png" data-src="y.png">') == "y.png"
    assert normalize_image_url("ftp://x/y.png") == ""


def test_downloaded_external_images_render_locally_with_anchor():
    digest = "ab" * 32
    mapping = {"https://mmbiz.qpic.cn/a.png": f"/media/remote/ab/{digest}.png"}
    markdown = str(render_body("![图](https://mmbiz.qpic.cn/a.png)\n\n![](https://other.example/b.png)", mapping))
    assert f'src="/media/remote/ab/{digest}.png"' in markdown and f'id="{image_anchor(digest)}"' in markdown
    assert "外部图片已省略" in markdown and "other.example" not in markdown
    html = str(render_body('<p><img data-src="https://mmbiz.qpic.cn/a.png" src="data:x"></p>', mapping, "html"))
    assert f'src="/media/remote/ab/{digest}.png"' in html


def test_downloads_only_reach_public_addresses():
    for address in ("10.1.2.3", "127.0.0.1", "192.168.1.1", "169.254.169.254", "100.101.102.103", "::1",
                    "::ffff:127.0.0.1", "fe80::1%eth0", "224.0.0.1", "0.0.0.0", "不是地址"):
        assert blocked_address(address), address
    assert not blocked_address("1.1.1.1") and not blocked_address("2606:4700:4700::1111")
    with pytest.raises(FetchError):
        download("file:///etc/passwd")
    with pytest.raises(FetchError):
        download("http://127.0.0.1:5432/a.png")
    # 跳转到 http(s) 以外的地址不跟。
    request = urllib.request.Request("https://mmbiz.qpic.cn/a.png")
    with pytest.raises(FetchError):
        _Redirects().redirect_request(request, None, 302, "Found", {}, "file:///etc/passwd")


def test_connection_to_a_private_peer_is_dropped():
    # 域名先解析到公网、建连接时却连到了内网（DNS rebinding、跳转）：建连接这一步就断开。
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        with pytest.raises(FetchError):
            _checked_create_connection(server.getsockname(), 3)
    finally:
        server.close()


def test_image_extension_sniffs_content():
    assert image_extension(b"\x89PNG\r\n\x1a\n....") == ".png"
    assert image_extension(b"\xff\xd8\xff\xe0....") == ".jpg"
    assert image_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
    assert image_extension(b"<html>", "text/html") == ""
    assert image_extension(b"????", "image/jpeg") == ".jpg"


def test_ocr_text_order_and_chunks():
    items = [
        [[[300, 12], [420, 12], [420, 40], [300, 40]], "1234 亿元", 0.98],
        [[[10, 10], [280, 10], [280, 42], [10, 42]], "广告收入", 0.99],
        [[[10, 60], [400, 60], [400, 92], [10, 92]], "主站货币化率见顶", 0.95],
        [[[10, 100], [80, 100], [80, 120], [10, 120]], "噪", 0.2],
    ]
    assert order_text(items) == "广告收入  1234 亿元\n主站货币化率见顶"
    # Horizontal placement alone does not provide speaker/color evidence.
    sided = [
        [[[20, 80], [120, 80], [120, 110], [20, 110]], "姜禄彦", 0.99],
        [[[20, 120], [280, 120], [280, 160], [20, 160]], "古茗店效还行，折扣不能再深了", 0.99],
        [[[250, 200], [390, 200], [390, 240], [250, 240]], "同店还有压力", 0.99],
    ]
    ordered = order_text(sided)
    assert ordered.splitlines()[-1] == "同店还有压力"
    assert SIDE_SELF not in ordered
    assert "方伟：同店还有压力" not in present_ocr_pieces([ordered])

    class UniformPage:
        shape = (300, 500, 3)

        def __getitem__(self, point):
            return (237, 237, 237)

    unknown = order_text(sided, rgb_image=UniformPage())
    assert all(line.startswith(BUBBLE_UNKNOWN) for line in unknown.splitlines())
    assert "方伟：" not in present_ocr_pieces([unknown])
    digest = "cd" * 32
    chunks = ocr_chunks([(digest, "拼多多二季度营收同比增长\n广告收入 1234 亿元"), (digest, "重复的图不算第二次"),
                         ("ef" * 32, "装饰")])
    assert [anchor for anchor, _text in chunks] == [image_anchor(digest)]
    assert "1234" in chunks[0][1]
    assert ocr_chunks([("aa" * 32, "字" * (OCR_MIN_CHARS - 1))]) == []

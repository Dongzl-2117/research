from researchlib.boilerplate import Profile, clean_body, normalize, strip_meta

HEADER = """# 标题

- 作者：投资向善
- 时间：2026-10-14 07:00:00 (UTC+8)
- sn：`a6fadfa7`
- 原链接：https://mp.weixin.qq.com/s?sn=a6
- 原始数据：[raw/articles/a6.json](../../raw/articles/a6.json)

正文第一句，讲腾讯的渠道优势。
"""

ZSXQ = """# 帖子

- 作者：王楠
- 时间：2025-11-13 23:07 (UTC+8)
- 类型：talk
- 点赞 2 · 评论 1 · 阅读人数 608
- topic_id：`55188` · 原链接：https://wx.zsxq.com/topic/55188

能不能聊一聊新氧

## 评论（1）

- **方伟Rey**（2025-11-13 23:15）
  其实是个风投逻辑
"""

TAIL = [
    "适合认可巴菲特、",
    "价值投资理念的朋友加入，",
    "我们只在能力圈范围内讨论企业的商业模式",
    "我也会在朋友圈分享我对企业的思考判断。",
]


def _profile(lines=(), images=()):
    return Profile({normalize(line) for line in lines}, set(images))


def test_meta_header_is_removed():
    out = strip_meta(HEADER)
    assert "sn：" not in out and "原链接" not in out and "原始数据" not in out and "作者：" not in out
    assert "正文第一句" in out and out.startswith("# 标题")


def test_zsxq_header_removed_and_comments_kept():
    out = clean_body(ZSXQ)
    assert "点赞" not in out and "topic_id" not in out
    assert "## 评论（1）" in out and "风投逻辑" in out


def test_meta_like_line_inside_body_is_kept():
    body = "# t\n\n正文开头。\n\n- 作者：这一行在正文中间，不是归档头\n"
    assert "这一行在正文中间" in clean_body(body)


def test_recruiting_tail_is_cut_even_when_split_into_fragments():
    body = "\n\n".join(
        ["# t", "腾讯的渠道优势是零下载成本，能快速转化存量用户。" * 6, "— 加入会员群—", "*"]
        + TAIL
        + ["，同时你", "也可以选择私信我（", "加我：woshirey23", "备注：群会员", "）", "所以白嫖勿扰。", "![](qr.jpg)"]
    )
    out = clean_body(body, _profile(TAIL, {"h-qr"}), {"qr.jpg": "h-qr"})
    assert "渠道优势" in out
    for word in ("会员群", "woshirey23", "白嫖", "qr.jpg", "能力圈"):
        assert word not in out


def test_template_image_removed_but_content_image_kept():
    body = "# t\n\n正文。\n\n![](chart.png)\n\n更多正文分析在这里。\n\n![](ad.jpg)\n\n结尾的判断。\n"
    out = clean_body(body, _profile((), {"h-ad"}), {"chart.png": "h-chart", "ad.jpg": "h-ad"})
    assert "chart.png" in out and "ad.jpg" not in out and "结尾的判断" in out


def test_promo_keyword_alone_does_not_cut_real_content():
    body = "# 二维码支付\n\n" + "\n\n".join(
        ["中国的移动支付靠扫码普及，二维码成本极低。" * 3, "印度：UPI QR 直接连银行账户。", "东南亚：扫码跨境互联。"]
    )
    assert clean_body(body, _profile()).count("扫码") >= 2


def test_repeated_headings_without_promo_are_kept():
    heading = "一、核心观点 Core Viewpoints"
    body = f"# t\n\n## {heading}\n\n这家公司靠定价权赚钱。\n"
    assert heading in clean_body(body, _profile([heading]))

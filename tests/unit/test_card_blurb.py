from researchlib.textutil import argumentative_excerpt


def test_excerpt_skips_sn_likes_and_timestamp():
    body = """sn: 8821
likes: 14
comments: 2
2024-10-19 13:29

这篇把广告加载率下降写成了核心判断。后面是展开。
"""
    text = argumentative_excerpt(body, title="加载率")
    assert text.startswith("这篇把广告加载率下降写成了核心判断")
    assert "likes" not in text
    assert "sn:" not in text
    assert "13:29" not in text


def test_excerpt_strips_inline_meta_prefix():
    body = "2024-10-19 13:29 sn: 9 likes: 1 这篇认为价格会松，不是流水账。"
    text = argumentative_excerpt(body)
    assert text.startswith("这篇认为价格会松")
    assert "likes" not in text


def test_excerpt_skips_chinese_counters():
    body = "点赞 12\n评论 3\n阅读 100\n\n作者把广告业务当成这篇的主体。"
    assert argumentative_excerpt(body) == "作者把广告业务当成这篇的主体。"


def test_excerpt_keeps_a_year_inside_the_claim():
    body = "2024年产能会下来，价格跟着松。"
    assert argumentative_excerpt(body).startswith("2024年产能会下来")


def test_excerpt_drops_title_and_returns_blank_for_meta_only():
    text = argumentative_excerpt("标题\n\n正文说明了渠道库存为什么升高。", title="标题")
    assert text.startswith("正文说明了渠道库存")
    assert argumentative_excerpt("sn: 1\nlikes: 2\n13:29\n") == ""


def test_excerpt_skips_wechat_archive_header():
    body = """作者：方伟
时间：2024-10-19 13:29
原文链接：https://mp.weixin.qq.com/s/abc123
来源：微信公众号

广告加载率下降，是这篇真正的判断。后面再展开渠道。
"""
    text = argumentative_excerpt(body, title="加载率下降")
    assert text == "广告加载率下降，是这篇真正的判断。"
    assert "作者" not in text
    assert "原文链接" not in text
    assert "mp.weixin.qq.com" not in text
    assert "13:29" not in text


def test_excerpt_skips_zsxq_header_on_one_line():
    body = """标题：渠道库存为什么升高
作者：林文丰 时间：2024年5月1日 21:30 原链接：https://articles.zsxq.com/id_abc
https://mp.weixin.qq.com/s/only-url
点赞：12
评论：3
阅读：100
在看：7
sn: 4412
likes: 9

渠道库存升高，是因为经销商在等价格。
"""
    text = argumentative_excerpt(body, title="渠道库存为什么升高")
    assert text == "渠道库存升高，是因为经销商在等价格。"
    assert "原链接" not in text
    assert "zsxq" not in text
    assert "在看" not in text
    assert "likes" not in text


def test_excerpt_skips_generic_leading_key_value_and_url_line():
    body = """阅读量：3201
发布时间：2024-10-19 13:29
source: 知识星球
www.example.com/archive

这篇认为价格会松，不是流水账。
"""
    text = argumentative_excerpt(body)
    assert text.startswith("这篇认为价格会松")
    assert "阅读量" not in text
    assert "发布时间" not in text
    assert "example.com" not in text


def test_excerpt_keeps_a_claim_that_uses_a_colon():
    body = "核心判断：广告加载率在下降，不是偶尔波动。"
    assert argumentative_excerpt(body) == "核心判断：广告加载率在下降，不是偶尔波动。"


def test_excerpt_keeps_title_sentence_that_does_not_repeat_the_title():
    body = "标题：这篇文章真正的判断是价格会松。\n\n后面还有展开。"
    text = argumentative_excerpt(body, title="渠道库存")
    assert text.startswith("标题：这篇文章真正的判断是价格会松")


def test_excerpt_chinese_header_only_is_blank():
    body = "作者：方伟\n时间：2024-10-19 13:29\n原链接：https://mp.weixin.qq.com/s/abc\n"
    assert argumentative_excerpt(body, title="加载率") == ""


def test_excerpt_strips_chinese_header_prefix_on_the_same_line():
    body = (
        "作者：方伟 时间：2024-10-19 13:29 原链接：https://mp.weixin.qq.com/s/abc "
        "这篇把广告加载率下降写成了核心判断。"
    )
    text = argumentative_excerpt(body, title="加载率下降")
    assert text == "这篇把广告加载率下降写成了核心判断。"
    assert "作者：" not in text
    assert "原链接" not in text
    assert "13:29" not in text


def test_excerpt_keeps_short_claim_without_a_period():
    assert argumentative_excerpt("核心判断：广告加载率在下降").startswith("核心判断：")

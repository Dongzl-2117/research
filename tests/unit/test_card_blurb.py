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

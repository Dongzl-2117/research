from researchlib.boilerplate import (
    Profile,
    clean_body,
    clean_html_document,
    is_cta_line,
    is_promo_paragraph,
    looks_like_qr,
    normalize,
    strip_meta,
)

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
    # 从公众号原文页取的正文多一行「图片位置：原文页」，同样是归档头。
    page = HEADER.replace("\n\n正文第一句", "\n- 图片位置：原文页\n\n正文第一句")
    assert strip_meta(page) == out


def test_rss_link_meta_line_is_removed():
    """Stratechery 归档头的 `- rss_link:` 和 `- source:` 一样，正文开始前整行去掉。"""
    body = (
        "# 标题\n\n"
        "- source: member-rss\n"
        "- rss_link: https://stratechery.com/?access_token=example\n"
        "\n"
        "正文第一句，讲渠道优势。\n"
    )
    stripped = strip_meta(body)
    assert "rss_link" not in stripped and "access_token" not in stripped and "member-rss" not in stripped
    assert "正文第一句" in stripped and stripped.startswith("# 标题")
    cleaned = clean_body(body)
    assert "rss_link" not in cleaned and "access_token" not in cleaned and "member-rss" not in cleaned
    assert "正文第一句" in cleaned


BARE_HEADER = """# 渠道怎么看

作者：方伟
时间：2026-10-14 07:00:00
原文链接：https://mp.weixin.qq.com/s/abc
阅读：1234

腾讯的渠道优势是零下载成本，能快速转化存量用户。
"""


def test_bare_archive_header_without_dashes_is_removed():
    out = strip_meta(BARE_HEADER)
    assert "作者：" not in out and "mp.weixin.qq.com" not in out and "阅读：" not in out
    assert "渠道优势" in out and out.startswith("# 渠道怎么看")


def test_zsxq_header_removed_and_comments_kept():
    out = clean_body(ZSXQ)
    assert "点赞" not in out and "topic_id" not in out
    assert "## 评论（1）" in out and "风投逻辑" in out


LONGFORM = """# 后发制人是优秀组织的试金石

- 作者：王小明
- 时间：2025-02-23 10:24 (UTC+8)
- 类型：talk
- topic_id：`1525` · 原链接：https://wx.zsxq.com/topic/1525
- 原始数据：[raw/topics/1525.json](../../raw/topics/1525.json)

> 以下为话题摘要，全文见下方「长文」。

**后发制人是优秀组织的试金石**

上个周末，腾讯旗下的微信搜索接入了新模型，这是一个影响深远的事情...

## 长文：后发制人是优秀组织的试金石

原文链接：https://articles.zsxq.com/id_abc.html · 原始 HTML：[raw/articles/abc.html](../../raw/articles/abc.html)

上个周末，腾讯旗下的微信搜索接入了新模型，这是一个影响深远的事情，意味着腾讯找到了切入方式。

第二段讲组织能力。

## 评论（1）

- **李四**（2025-02-23 11:00）
  写得好
"""


def test_zsxq_longform_scaffold_is_dropped():
    out = clean_body(LONGFORM)
    assert "以下为话题摘要" not in out and "原始 HTML" not in out and "raw/articles" not in out
    assert "长文：" not in out and "**后发制人" not in out
    # 摘要和长文开头是同一句，只留长文里那一遍。
    assert out.count("上个周末") == 1 and "意味着腾讯找到了切入方式" in out and "第二段讲组织能力" in out
    assert "## 评论（1）" in out and "写得好" in out


def test_zsxq_longform_heading_kept_when_titles_differ():
    body = LONGFORM.replace("**后发制人是优秀组织的试金石**", "**周末随手记**")
    out = clean_body(body)
    assert "## 长文：后发制人是优秀组织的试金石" in out
    assert "以下为话题摘要" not in out and "周末随手记" not in out and "raw/articles" not in out


def test_zsxq_longform_source_line_dropped_without_summary_note():
    source = "原文链接：https://articles.zsxq.com/id_abc.html · 原始 HTML：[raw/articles/abc.html](x)"
    out = clean_body(f"# t\n\n## 长文：标题\n\n{source}\n\n正文。\n")
    assert "## 长文：标题" in out and "正文。" in out and "raw/articles" not in out


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


def test_cta_tail_and_qr_images_drop_without_a_repeat_profile():
    body = "\n\n".join(
        [
            "# 渠道怎么看",
            "腾讯的渠道优势是零下载成本，能快速转化存量用户。这段要留在正文里，讲的是商业模式而不是文末号召。",
            "![](chart.png)",
            "再补一句：广告库存还在抬升，不是文末号召。",
            "适合认可巴菲特、",
            "价值投资理念的朋友加入，",
            "我们只在能力圈范围内讨论企业的商业模式",
            "## 招募",
            "长按识别二维码关注公众号",
            "加微信：example_id",
            "所以白嫖勿扰。",
            "![](images/qrcode.jpg)",
            '![](images/wechat-qr.png "二维码")',
        ]
    )
    out = clean_body(body)
    assert "渠道优势" in out and "chart.png" in out and "广告库存还在抬升" in out
    for word in ("招募", "长按识别", "qrcode", "wechat-qr", "白嫖", "加微信", "能力圈", "巴菲特"):
        assert word not in out


def test_html_article_drops_header_qr_and_recruiting():
    html = """
<p>作者：方伟</p>
<p>时间：2026-10-14 07:00:00</p>
<p>原文链接：https://mp.weixin.qq.com/s/abc</p>
<p>腾讯的渠道优势是零下载成本，能快速转化存量用户。这段要留在正文里，讲的是商业模式而不是文末号召。</p>
<p><img src="https://mmbiz.qpic.cn/chart.png" alt="渠道图"></p>
<p>再补一句：广告库存还在抬升，不是文末号召。</p>
<h2>招募</h2>
<p>长按识别二维码关注公众号</p>
<p>加微信：example_id</p>
<img src="https://mmbiz.qpic.cn/qrcode.jpg" alt="二维码">
"""
    out = clean_html_document(html)
    assert "渠道优势" in out and "chart.png" in out and "广告库存还在抬升" in out
    for word in ("作者：", "mp.weixin.qq.com", "招募", "长按识别", "qrcode", "加微信"):
        assert word not in out


def test_repeated_headings_without_promo_are_kept():
    heading = "一、核心观点 Core Viewpoints"
    body = f"# t\n\n## {heading}\n\n这家公司靠定价权赚钱。\n"
    assert heading in clean_body(body, _profile([heading]))


# —— 推销写成整段长句、句子里只是提到平台名（2026-11）——
# 微信号都是虚构的。

PITCH = [
    "— 加入会员群—",
    "*适合认可巴菲特、段永平价值投资理念的朋友加入，我们只在能力圈范围内讨论企业的商业模式、企业文化、"
    "战略方向、业务增长情况以及有可能的风险，拒绝投机，拒绝看图看线，同时你也可以选择私信我"
    "（加我：abc_123 备注：群会员）",
    "*我也会在朋友圈分享我对企业的思考判断。我的时间宝贵，只跟群会员沟通，我从来不占别人便宜，"
    "也不想别人占我便宜，所以白嫖勿扰，感谢理解。",
]


def test_pitch_written_as_long_paragraphs_is_cut():
    body = "\n\n".join(
        [
            "# t",
            "腾讯的渠道优势是零下载成本，能快速转化存量用户。" * 4,
            "看样子还得再等十年，这个竞争格局一直都不太稳定。",
        ]
        + PITCH
    )
    out = clean_body(body)
    assert "渠道优势" in out and "竞争格局一直都不太稳定" in out
    for word in ("会员群", "abc_123", "白嫖", "能力圈", "朋友圈"):
        assert word not in out


def test_pitch_paragraph_without_a_heading_is_cut():
    body = "\n\n".join(
        [
            "# t",
            "长期看好有定价权的公司，护城河决定下限，扩张性决定上限。" * 3,
            "有问题交流可以在星球里发帖提问，只讨论长期价值，不讨论短期价格波动，加社群后也可直接加我微信沟通abc_123。",
        ]
    )
    out = clean_body(body)
    assert "护城河决定下限" in out
    assert "星球里发帖" not in out and "abc_123" not in out


def test_handle_in_brackets_is_dropped_and_sentence_kept():
    body = (
        "# t\n\n"
        + "长期看好有定价权的公司。" * 4
        + "\n\n你说老巴接下来会干嘛，他在等待，等早已看好的公司跌到心仪的价格，然后狠狠地买入。（微信交流：abc_123）\n"
    )
    out = clean_body(body)
    assert "然后狠狠地买入。" in out
    assert "abc_123" not in out and "微信交流" not in out


def test_sentences_before_the_pitch_in_the_same_line_are_kept():
    line = (
        "最后就是我在社群团购和短视频的经验，所以关键词就包括公私域、社交传播、小B等等。"
        "目前有一些方向，还需要进一步调研，感兴趣的同学可以加微信聊聊（微信：abc_123）"
    )
    out = clean_body("# t\n\n" + "产品经理要先懂用户。" * 6 + "\n\n" + line + "\n")
    assert "所以关键词就包括公私域、社交传播、小B等等。" in out
    assert "感兴趣的同学" not in out and "abc_123" not in out


def test_platform_names_inside_a_sentence_are_not_a_call_to_action():
    for line in (
        "知识星球是内耗型的产品，不是分享型的，一个星主没有其他流量来源的话，最终会走向死寂",
        "（1）知识星球的20%抽成",
        "去年有一个人，加我微信后直接给我转了小一万块钱，然后聊了 2 小时",
        "成功白嫖特殊福利",
        "就是视频号直播打赏（微信豆消费）以及微信小游戏平台分成",
    ):
        assert not is_cta_line(line), line
    for line in ("— 加入会员群—", "推荐阅读", "长按识别二维码关注", "点击关注公众号", "所以白嫖勿扰。"):
        assert is_cta_line(line), line
    assert is_cta_line("加我：abc_123")


def test_research_that_mentions_payment_qr_codes_is_not_a_pitch():
    for line in (
        "中国的移动支付靠扫码普及，二维码成本极低，商家贴一张码就能收款，这是东南亚学的样板。",
        "我们线下交流群前天有人问，腾讯的广告库存还能涨多少，我的回答是视频号的商业化刚开始。",
    ):
        assert not is_promo_paragraph(line), line
    assert is_promo_paragraph(PITCH[1]) and is_promo_paragraph(PITCH[2])


def test_short_post_that_names_the_platform_is_kept():
    body = "\n\n".join(
        [
            "# 我发现，知识星球为了节省成本，现在把图片搞得特别模糊",
            "我发现，知识星球为了节省成本，现在把图片搞得特别模糊，已经严重影响用户体验了。",
            "你看吧，不到一个月，又得返工",
            "![](shot.png)",
        ]
    )
    out = clean_body(body, _profile(), {"shot.png": "h-shot"})
    for word in ("严重影响用户体验", "又得返工", "shot.png"):
        assert word in out


def test_split_research_sentence_before_the_pitch_is_kept():
    # 老文章一句正文被拆成几行碎句；连起来没有推销信号的留下，后面的推销照样切。
    body = "\n\n".join(
        [
            "# t",
            "阿里的回购会抵消一部分稀释，但利润增速跟不上收入增速。" * 3,
            "最后你的每股收益 EPS 的增速只有不到 10%，而不是 20%",
            "，这你是需要知道的。",
        ]
        + PITCH
    )
    out = clean_body(body)
    assert "而不是 20%" in out and "这你是需要知道的" in out
    assert "会员群" not in out and "abc_123" not in out


def test_pitch_section_under_a_title_is_cut_to_the_end():
    body = "\n\n".join(
        [
            "# t",
            "云计算公司要获得超额利润，必须把供给侧的几座大山推平。" * 3,
            "— 扫码下方微信加入会员群—",
            "*适合认可巴菲特、段永平价值投资理念的朋友加入，同时你也可以选择直接私信我问题。（备注：群会员）",
            "*我也会在朋友圈分享我对企业的思考判断。我的时间宝贵，我也是个诚信本分人，所以非诚勿扰。",
            "*新用户加入免费赠送价值365元的知识星球，你也可以选择直接购买，限时优惠66元：",
            "**— 扫码下方微信加入线下交流群 —**",
            "我不是一个善于拒绝的人，但个人时间有限，线下约见面或者电话会独占我的时间，所以增加一个更高门槛的群服务",
            "**【会员权益】**",
            "1）线下一对一交流或者线上电话：以北上广深杭为主，杭州与广州可以常约；",
            "2）有问必回：群里的问题，除非不懂，要不然都会回；",
            "3）半年已超 70 人加入，我尽可能做到让群友都互相认识，线下多聚，跨行业可以深度交流。",
        ]
    )
    out = clean_body(body)
    assert "超额利润" in out
    for word in ("会员群", "交流群", "会员权益", "有问必回", "半年已超"):
        assert word not in out


def test_call_to_action_title_mid_article_does_not_swallow_later_sections():
    body = "\n\n".join(
        [
            "# t",
            "第一部分讲渠道，腾讯的渠道优势是零下载成本。" * 3,
            "— 扫码加入社群 —",
            "## 第二部分",
            "第二部分讲估值，市盈率在历史低位，分红率还在抬升，回购也在加速。" * 3,
        ]
        + PITCH
    )
    out = clean_body(body)
    assert "第二部分讲估值" in out and "会员群" not in out


def test_call_to_action_title_mid_article_without_headings_keeps_the_analysis():
    body = "\n\n".join(
        ["# t", "第一部分讲渠道，腾讯的渠道优势是零下载成本。" * 3, "— 扫码加入社群 —"]
        + [f"第{n}段讲估值，市盈率在历史低位，分红率还在抬升，回购也在加速，现金流足够覆盖。" * 2 for n in range(6)]
        + PITCH
    )
    out = clean_body(body)
    assert out.count("讲估值") == 12 and "会员群" not in out


def test_picture_post_keeps_its_picture_after_a_call_to_action():
    body = "\n\n".join(["# 群友提问", "长按识别二维码关注", "![](answer.png)"])
    out = clean_body(body, _profile(), {"answer.png": "h-answer"})
    assert "answer.png" in out and "长按识别" not in out


def test_picture_after_a_call_to_action_dropped_when_the_post_has_text():
    body = "\n\n".join(
        [
            "# t",
            "腾讯的广告库存还在抬升，视频号的商业化刚刚开始，利润率还有空间。" * 2,
            "**— 扫码下方微信加入微信会员群 —**",
            "![](group.jpg)",
        ]
    )
    out = clean_body(body, _profile(), {"group.jpg": "h-group"})
    assert "广告库存" in out and "group.jpg" not in out


def test_paragraph_that_only_mentions_a_promo_word_survives_the_template_tail():
    promo = [
        "关于付费星球",
        "长文表达比较消耗心力，日常我更偏爱在知识星球输出，欢迎加入一起讨论。",
        "我分享的内容以产品设计、商业创业居多，更适合理性客观、有好奇心的朋友。",
        "关于产品导师服务",
        "我有信心在产品、在创业上给到你帮助，名额有限，扫码预约。",
    ]
    body = "\n\n".join(
        [
            "# t",
            "交易的纠纷往往来自条款模棱两可，人都会高估自己的付出。" * 4,
            "赤裸裸地先聊条款，确实不是我们的文化，我原本也这样，但我是吃过亏的，"
            "前段时间被一家企业白嫖了两次，后面我才发现先聊条款最省事",
            "亲兄弟，明算账，先小人，后君子，像一个生意人一样思考，可不能有书生气呀。",
        ]
        + promo
    )
    out = clean_body(body, _profile(promo))
    assert "白嫖了两次" in out and "亲兄弟" in out
    assert "付费星球" not in out and "导师服务" not in out


def test_html_article_drops_pitch_paragraphs():
    html = "".join(
        [
            "<p>腾讯的渠道优势是零下载成本，能快速转化存量用户。这段要留在正文里，讲的是商业模式。</p>",
            "<p>看样子还得再等十年，这个竞争格局一直都不太稳定，我们继续跟踪。</p>",
        ]
        + [f"<p>{line}</p>" for line in PITCH]
    )
    out = clean_html_document(html)
    assert "渠道优势" in out and "竞争格局" in out
    for word in ("会员群", "abc_123", "白嫖"):
        assert word not in out


# —— 统计切尾不吞正文、写法特殊的推销、文首号召（2026-11-11）——


def test_template_tail_keeps_text_between_misplaced_promo_images():
    # 老文章按段估过图的位置，文末的入群码、优惠券图插进了最后两段正文中间。统计切尾从这张图起切的话，
    # 容差会把后面两段正文一起切掉；号召之前的整段正文要留下，夹在中间的模板图照样去掉。
    promo = [
        "— 扫码下方微信加入微信会员群 —",
        "我们只在能力圈范围内讨论企业的商业模式、企业文化和战略方向",
        "【我的持仓】",
        "腾讯：买入后一直拿着，内在价值预估还有空间，仓位过半",
        "美团：继续跌就继续加，内在价值预估还有空间",
        "*仅供参考，不构成任何投资建议",
        "有问题咨询可以在星球里发帖提问，只回答付费会员的问题",
        "备注：群会员",
    ]
    body = "\n\n".join(
        [
            "# t",
            "巴菲特一直偏爱低杠杆、低市盈率、高分红的公司，这一点几十年都没有变过。" * 4,
            "![](group.jpg)",
            "一个人能跑赢六十年的周期，我实在想不到任何理由不去学习研究他，反而去学各种奇技淫巧。",
            "![](coupon.jpg)",
            "选股标准一篇文章讲不完，这次主要讲高分红，我也懒得再写一遍了，直接贴知识星球昨晚的问答吧：",
        ]
        + promo
    )
    profile = _profile(promo, {"h-group", "h-coupon"})
    out = clean_body(body, profile, {"group.jpg": "h-group", "coupon.jpg": "h-coupon"})
    assert "跑赢六十年的周期" in out and "直接贴知识星球昨晚的问答" in out
    for word in ("group.jpg", "coupon.jpg", "会员群", "能力圈", "持仓", "仅供参考"):
        assert word not in out


def test_pitch_under_a_plain_label_is_cut_to_the_end():
    body = "\n\n".join(
        [
            "# t",
            "电商项目能不能做，先看品类有没有局部优势，再看市场还有多少增量空间。" * 3,
            "以上就是我判断一个电商项目能不能做的思路，希望对你有用，晚安～",
            "关于付费星球",
            "长文表达比较消耗心力，日常我更偏爱在知识星球输出，每天能稳定输出十多条。",
            "我分享的内容以产品设计、商业创业居多，更合适理性客观的同学，欢迎感兴趣的同学加入，交个朋友。",
            "关于产品导师服务",
            "我有信心在产品、在创业上帮助进取欲望强的同学少走弯路，加速成长，也想把自己打造成一款产品。",
            "（2）商业咨询服务以问题为核心，方式则以微信与语音为主，你有问题，我引导你去解决。",
            "（3）按半年期付费，满意就续费，不满意就相忘于江湖；半年期4500，一年期8800；",
        ]
    )
    out = clean_body(body)
    assert "局部优势" in out and "晚安" in out
    for word in ("付费星球", "导师服务", "欢迎感兴趣", "半年期"):
        assert word not in out


def test_plain_label_followed_by_analysis_is_kept():
    # 「关于知识星球」后面是分析、没有一句冲着读者说的话，不当推销。
    body = "\n\n".join(
        [
            "# t",
            "内容付费平台要解决的是创作者的流量来源问题，平台本身并不分配流量。" * 3,
            "关于知识星球",
            "知识星球是内耗型的产品，不是分享型的，一个星主没有其他流量来源的话，社群最终会走向死寂。",
            "知识星球的抽成是 20%，比别家都高，但工具更完整，更适合已经有一批忠实读者、需要收费工具的作者。",
        ]
    )
    out = clean_body(body)
    assert "关于知识星球" in out and "最终会走向死寂" in out and "抽成是 20%" in out


def test_invitations_to_the_authors_community_at_the_end_are_cut():
    text = "新功能上线后，应该第一时间与目标用户取得联系，听取他们的感受，再看数据。" * 3
    closing = "以上就是我做产品的十条原则，希望对你有用哈。"
    out = clean_body("\n\n".join(["# t", text, closing, "对产品感兴趣的同学，还可以到我的星球玩哈："]))
    assert "希望对你有用哈" in out and "我的星球" not in out
    out = clean_body(
        "\n\n".join(["# t", text, "——交流社群——", "有问题交流可以在星球里发帖提问，只讨论价值，不讨论价格。"])
    )
    assert "取得联系" in out and "交流社群" not in out and "星球里发帖" not in out


def test_reader_tips_under_the_title_are_dropped():
    tips = (
        "tips：致读者朋友，由于微信的推送规则，即使你关注了我，可能也常常收不到推送，"
        "可以点击名片，设为星标⭐️，文章每天才会自动推送的。"
    )
    body = "\n\n".join(
        [
            "# t",
            tips,
            "“最好的生意，是那种能在很长时期内，以非常高的回报率投入大量增量资本的生意。”",
            "苹果的商业模式是躺赚的，硬件、服务和生态互相加强。" * 3,
        ]
    )
    out = clean_body(body)
    assert out.startswith("# t") and "最好的生意" in out and "躺赚" in out
    assert "致读者朋友" not in out and "星标" not in out


def test_self_declared_ad_with_a_handle_is_cut_but_talk_about_ads_is_kept():
    body = "\n\n".join(
        [
            "# t",
            "片仔癀跌了那么多，市盈率居然还有 44 倍，投资一看模式，二看团队，三看价格。" * 2,
            "最后打个广告，最近在调研一家商业地产公司，期待这个行业的资深人士与我沟通联系，"
            "我的微信是 abc_123，备注：商业地产。",
        ]
    )
    out = clean_body(body)
    assert "片仔癀" in out and "打个广告" not in out and "abc_123" not in out
    assert not is_promo_paragraph("平台内的业务导流，要比在其他地方打广告便宜 70%，而且自带信任。")


def test_long_alt_mentioning_qr_code_is_a_caption_not_a_promo_code():
    # 说明句里提到 QR code 的截图是正文（Stratechery 讲产品配对流程），短标签「二维码」才是引流码。
    caption = "When you launch the Mac App, it prompts you to login on your phone (pairing is done with a QR code)"
    assert not looks_like_qr("https://example.com/login.png", caption)
    assert looks_like_qr("https://example.com/a.png", "二维码")
    assert looks_like_qr("images/qrcode.jpg")

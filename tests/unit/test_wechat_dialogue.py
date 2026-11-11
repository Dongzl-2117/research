"""行首说话人（split_speaker）和截图在阅读页的样子。

正文、评论里行首的「昵称：」加粗，财务指标、字段名这类冒号前的字不算说话人。
截图在阅读页留原图，识图的字不铺进正文；图片帖的标题用图里那句话，不用说话人。
谁说的话由识图标好（见 test_ocr_units），这里不再从自由文字里猜。
"""

import pytest

from researchlib.render import image_anchor, inline_image_ocr, render_body
from researchlib.wechat_dialogue import split_speaker
from researchlib.zsxq_title import zsxq_display_title

# 文章 14886 的形状：正文一句，判断在两张重叠的微信群截图里。昵称和对话都是编的。
EXPECTED = (
    "方伟：瑞幸和古茗都算头部，但瑞幸同店还有压力。\n"
    "\n"
    "王小明：古茗店效还行，瑞幸的折扣不能再深了。\n"
    "\n"
    "李小红：先看到这里，不展开了。\n"
    "\n"
    "方伟：同店压力还在，折扣不能再深\n"
    "会员那边先不动。"
)

AMAZON_PRODUCT = """14:10  e  4G
Q  运动水杯1L  回  O
★★★★&Up  AllDiscounts  Up to $10
Sponsored O|9K+boughtfrom thisbrand inpast month
Limitedtimedeal  $1499 $16.99  $1699
$1069  Typical: $12.99
FREEdeliveryOct26-Nov4
购物车"""

META_CHART = """Fiscal.ai  @Fiscalai·12小时  X
Meta Ads v.Google Search
META-AdvertisingRevenue(LTM)
$250B
GoogleSearch$243B
8  MetaAds$223B
BuiltwithFiscalaiMcP"""

REDDIT_COMMENT = """Templar-kun.3moago
I agree that for the same price, products from Temu will be a higher quality.
Share
Reply
Award
"""


def test_image_post_title_uses_the_sentence_not_the_speaker_label():
    title = zsxq_display_title("图片", body="（见图）\n", ocr=EXPECTED, when="2024-10-19")
    assert title.startswith("瑞幸和古茗都算头部")
    assert not title.startswith("方伟")
    assert "网友" not in title


def test_product_chart_and_comment_shots_stay_images():
    """商品图、图表、Reddit 评论楼在阅读页仍是图，识图的字不进版面。"""
    a, c, r = "e5" * 32, "ad" * 32, "01" * 32
    html = str(
        render_body(
            "![](a.png)\n\n![](c.png)\n\n![](r.png)\n",
            {
                "a.png": f"/media/x/ab/{a}.png",
                "c.png": f"/media/x/ab/{c}.png",
                "r.png": f"/media/x/ab/{r}.png",
            },
        )
    )
    page = inline_image_ocr(html, {a: AMAZON_PRODUCT, c: META_CHART, r: REDDIT_COMMENT})
    assert page == html
    for digest in (a, c, r):
        assert f'<img id="{image_anchor(digest)}"' in page
    assert "方伟：" not in page and "网友：" not in page
    assert "运动水杯" not in page and "Sponsored" not in page and "Builtwith" not in page
    assert "Templar" not in page and "I agree" not in page
    assert "ocr-shot" not in page and "ocr-link" not in page and "原图 1" not in page


@pytest.mark.parametrize("field", ["合同负债", "商誉减值", "研发支出", "公允价值变动收益", "期末存货"])
def test_report_fields_at_line_start_are_not_speakers(field):
    assert split_speaker(f"{field}：123.45亿元", names=True) is None


@pytest.mark.parametrize("field", ["净资产收益率", "资本开支", "营业成本", "研发费用", "净利润率", "2025年研发费用率"])
def test_financial_labels_are_not_speaker_prefixes_for_other_consumers(field):
    assert split_speaker(f"{field}：百分之十", names=True) is None


@pytest.mark.parametrize(
    "nickname",
    ["Alice", "Bob", "alice_27", "Kevin-7", "Alice🇫🇷", "José", "Zoë", "François", "AlexanderResearch", "alice.27"],
)
@pytest.mark.parametrize("speech", ["我认为可以提价。", "I think pricing can improve."])
def test_latin_nickname_keeps_its_speech(nickname, speech):
    assert split_speaker(f"{nickname}：{speech}", names=True) == (nickname, speech)


@pytest.mark.parametrize("label", ["UnknownMetric", "NewRatio", "Backlog", "Typical", "UnknownMetricRatio"])
def test_unknown_latin_metric_labels_are_not_speakers(label):
    assert split_speaker(f"{label}：123.45%", names=True) is None


@pytest.mark.parametrize(
    "label",
    ["Al", "O'Brien", "O’Connor", "ResearchNicknameBeyondTwentyCharacters", "Long" + "x" * 256],
)
@pytest.mark.parametrize("speech", ["我认为可以提价。", "123.45%"])
def test_unsupported_label_shapes_are_not_speakers(label, speech):
    assert split_speaker(f"{label}：{speech}", names=True) is None

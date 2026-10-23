"""Role and UI boundaries; snippets are synthetic unless explicitly stated."""

import pytest

from researchlib.ocr import compose_ocr_text
from researchlib.wechat_dialogue import (
    BUBBLE_GUEST,
    BUBBLE_SELF,
    parse_shot,
    present_ocr_pieces,
    present_stored_ocr,
)


@pytest.mark.parametrize("label", ["", "方伟：", "网友：", "未确认："])
def test_606_saved_ui_spellings_do_not_become_speech(label):
    # These exact UI spellings occur in the locally saved 606 OCR snapshot.
    # Surrounding speech is synthetic, not a reconstruction of the original.
    raw = (
        "方伟：前面的判断必须保留。\n"
        f"{label}01:07m\n"
        f"{label}看十年价值投资交流（499）\n"
        f"{label}01:19 m\n"
        "网友：后面的问题也必须保留。"
    )
    expected = "方伟：前面的判断必须保留。\n\n网友：后面的问题也必须保留。"
    assert present_stored_ocr(raw) == expected
    assert compose_ocr_text([raw]) == expected


@pytest.mark.parametrize("label", ["方伟：", "网友：", "未确认："])
def test_stale_speaker_labels_do_not_protect_follow_counters_or_buttons(label):
    raw = (
        "方伟：真实正文还在。\n"
        f"{label}71个朋友关注\n"
        f"{label}82 455 38 33。\n"
        f"{label}分享21 推荐0 划线人数0\n"
        f"{label}关注同城 朋友 推荐\n"
        "网友：后面正文仍然保留。"
    )
    assert present_stored_ocr(raw) == "方伟：真实正文还在。\n\n网友：后面正文仍然保留。"


@pytest.mark.parametrize("marker,speaker", [(BUBBLE_SELF, "方伟"), (BUBBLE_GUEST, "网友")])
def test_confirmed_bubble_can_contain_a_time_or_follow_count(marker, speaker):
    raw = marker + "15:30\n" + marker + "71个朋友关注"
    assert present_ocr_pieces([raw]) == f"{speaker}：15:30\n71个朋友关注。"


def test_repeating_another_speakers_sentence_preserves_both_attributions():
    raw = "网友：零售的本分是帮消费者省钱。\n方伟：零售的本分是帮消费者省钱。"
    expected = "网友：零售的本分是帮消费者省钱。\n\n方伟：零售的本分是帮消费者省钱。"
    assert present_stored_ocr(raw) == expected
    assert present_stored_ocr(expected) == expected


def test_cross_image_overlap_deduplication_is_scoped_to_speaker():
    sentence = "零售的本分是帮消费者省钱。"
    shown = present_ocr_pieces([
        f"网友：{sentence}",
        "公司 财年\n经营利润率 20%\n净利率 15%",  # media separates image families
        f"方伟：{sentence}",
        f"方伟：{sentence}\n下一句作者正文必须留下。",
    ])
    assert shown.count(sentence) == 2
    assert "网友：" + sentence in shown
    assert "方伟：" + sentence in shown
    assert "下一句作者正文必须留下。" in shown


def test_473_report_mentioning_wechat_is_not_a_fangwei_chat():
    # Abbreviated, literal lines from the locally saved 473 image-text snapshot.
    # The original is a Tencent report, not a chat screenshot.
    report = (
        "1Q2026业务回顾及展望\n"
        "我们的智能投放产品矩阵腾讯营销AIM+赋能了广告主营销服务投放金额的约30%，\n"
        "并在小游戏、短剧和微信小店广告主中获得了广泛应用。\n"
        "WorkBuddy已成为中国最受欢迎的效率Al智能体服务。"
    )
    assert parse_shot(report).kind == "prose"
    assert present_ocr_pieces([report]) == report
    assert "方伟：" not in present_ocr_pieces([report])


@pytest.mark.parametrize("raw", [
    "AI\nIT五十年四座高峰  2022~\nMobile\nWeb  2007\nPC  1995\n1975\n公众号·开柒",
    "中国2026上半年\n应用收入榜TOP8\nApp Store 收入榜(上半年)\n排名  应用名称  总收入（预估）  环比变化\n"
    "Shenzhen Tencent  $705.6M  346.5%\nTechnology (S...\n抖音",
    "平台  B段运费规则  平台现金补贴  流量激励  商家自由度\n"
    "拼多多  商家基本不用承担B段  全额补贴B段  强流量权重，很多  低，大盘规则约束强",
    "方伟看十年\n扫码领取优惠券\n仅限前1000名\n66元立减金\n心知识星球",
])
def test_473_infographics_wrapped_tables_and_coupon_card_do_not_become_dialogue(raw):
    # Literal abbreviations from local historical image_texts, not invented full images.
    assert parse_shot(raw).kind == "media"
    assert present_ocr_pieces([raw]) == ""


def test_a_conversation_about_margins_is_not_mistaken_for_a_table():
    raw = BUBBLE_SELF + "经营利润率只是一个指标，应该和增长一起看。"
    assert present_ocr_pieces([raw]) == "方伟：经营利润率只是一个指标，应该和增长一起看。"


def test_book_excerpt_reader_profile_does_not_claim_authorship_of_the_quote():
    # Shape observed on 606's two real reading cards; the quote is synthetic.
    raw = "Rey\n摘录于2026/10/4\n这是书籍作者的文字，不是读者自己的判断。\n书名\n其他作者\n微信读书"
    assert parse_shot(raw).kind == "media"
    assert present_ocr_pieces([raw]) == ""

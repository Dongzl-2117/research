"""Role and UI boundaries; snippets are synthetic unless explicitly stated."""

import pytest

from researchlib.chunks import ocr_chunks
from researchlib.ocr import compose_ocr_text
from researchlib.wechat_dialogue import (
    BUBBLE_GUEST,
    BUBBLE_HOST,
    BUBBLE_SELF,
    BUBBLE_UNKNOWN,
    REFERENCE_NOTE,
    SIDE_SELF,
    parse_shot,
    present_ocr_pieces,
    present_stored_ocr,
)


def test_ordinary_host_nickname_cannot_authenticate_a_different_colored_bubble():
    raw = (BUBBLE_UNKNOWN + "方伟\n" + BUBBLE_GUEST + "这一轮我认为利润率会上升。\n"
           + BUBBLE_SELF + "截图持有人的不同看法。")
    text = present_ocr_pieces([raw])
    assert "方伟：这一轮我认为利润率会上升。" in text
    assert "未确认：截图持有人的不同看法。" in text
    assert "方伟：截图持有人的不同看法。" not in text


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


@pytest.mark.parametrize("marker,speaker", [(BUBBLE_SELF, "未确认"), (BUBBLE_HOST, "方伟"), (BUBBLE_GUEST, "网友")])
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
])
def test_473_infographics_and_wrapped_tables_retain_neutral_reference_text(raw):
    # Literal abbreviations from local historical image_texts, not invented full images.
    shot = parse_shot(raw)
    assert shot.kind == "media" and shot.family == "reference"
    shown = present_ocr_pieces([raw])
    assert shown == REFERENCE_NOTE + "\n" + raw
    assert "方伟：" not in shown and "网友：" not in shown
    assert present_stored_ocr(shown) == shown


def test_a_conversation_about_margins_is_not_mistaken_for_a_table():
    raw = BUBBLE_SELF + "经营利润率只是一个指标，应该和增长一起看。"
    assert present_ocr_pieces([raw]) == "未确认：经营利润率只是一个指标，应该和增长一起看。"


def test_book_excerpt_reader_profile_does_not_claim_authorship_of_the_quote():
    # Shape observed on 606's two real reading cards; the quote is synthetic.
    raw = "Rey\n摘录于2026/10/4\n这是书籍作者的文字，不是读者自己的判断。\n书名\n其他作者\n微信读书"
    assert parse_shot(raw).kind == "media"
    shown = present_ocr_pieces([raw])
    assert shown == REFERENCE_NOTE + "\n" + raw
    assert "方伟：" not in shown and "网友：" not in shown


@pytest.mark.parametrize("raw", [
    "方伟看十年\n扫码领取优惠券\n仅限前1000名\n66元立减金\n心知识星球",
    "Following Shop For You\n170 million want TikTok\nHome Friends Inbox Profile",
    "TikTok\n86 Following Followers Likes\nFollow Message\n1234 Video views",
])
def test_coupon_and_short_video_chrome_do_not_enter_dialogue_or_search(raw):
    shot = parse_shot(raw)
    assert shot.kind == "media" and shot.family == "interface"
    assert present_ocr_pieces([raw]) == ""
    assert ocr_chunks([("a" * 64, raw)]) == []


def test_reference_chunks_keep_values_and_repeat_the_attribution_boundary():
    raw = (
        "平台  B段运费规则  平台现金补贴  流量激励  商家自由度\n"
        + "\n".join(f"拼多多  数据行{index}  补贴20%  此项为表格资料，不是作者发言。" for index in range(60))
    )
    chunks = ocr_chunks([("a" * 64, raw)])
    assert len(chunks) > 1
    assert all(text.startswith(REFERENCE_NOTE + "\n") for _, text in chunks)
    assert all("方伟：" not in text and "网友：" not in text for _, text in chunks)
    assert all(f"数据行{index}" in "\n".join(text for _, text in chunks) for index in range(60))
    assert "补贴20%" in chunks[-1][1]


def test_neutral_reference_header_does_not_make_a_watermark_searchable():
    assert ocr_chunks([("a" * 64, REFERENCE_NOTE + "\n仅有水印")]) == []


def test_606_social_metric_row_is_ui_only_in_a_post_context():
    raw = "这段真正的正文必须保留。\n197.4万 13.6万 16.5万\n手艺门 +关注\n那之后的正文也必须保留。"
    shown = present_ocr_pieces([raw])
    assert "197.4万" not in shown
    assert "这段真正的正文必须保留。" in shown and "那之后的正文也必须保留。" in shown
    spoken = BUBBLE_SELF + "197.4万 13.6万 16.5万\n" + "手艺门 +关注"
    assert "未确认：197.4万 13.6万 16.5万。" == present_ocr_pieces([spoken])
    assert present_ocr_pieces(["197.4万 13.6万 16.5万"]) == "197.4万 13.6万 16.5万"


def test_historical_position_marker_does_not_prove_a_green_author_bubble():
    raw = "微信\n" + SIDE_SELF + "这句历史上只因靠右被标成作者。"
    assert present_ocr_pieces([raw]) == "未确认：这句历史上只因靠右被标成作者。"


def test_explicit_author_reply_header_survives_body_color_changes_and_stops_at_guest_header():
    raw = ("3条留言V 写留言\n方伟在学习（作者）\n"
           + BUBBLE_SELF + "作者的第一段判断。\n"
           + BUBBLE_GUEST + "作者的续段判断。\n"
           + "张冠留言1次·关注8个月\n" + BUBBLE_SELF + "新的未确认截图发送方。")
    text = present_ocr_pieces([raw])
    assert "方伟：作者的第一段判断" in text
    assert "作者的续段判断" in text
    assert "未确认：新的未确认截图发送方。" in text
    assert "方伟：新的未确认截图发送方" not in text

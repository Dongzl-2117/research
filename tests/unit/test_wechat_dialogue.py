"""微信截图 OCR 整理成「方伟：」「网友：」。

14886 的生产库在 Access 后面，这里用业主描述的那张群聊：群名、价格、小程序、
设为群待办、电量和时间、方伟Rey / 姜禄彦 / 徐哲航 / Rey，以及两张图重叠的句子。
阅读页和 articles.ocr_text 走同一份结果。
"""

from researchlib.ocr import append_ocr, compose_ocr_text
from researchlib.render import image_anchor, inline_image_ocr, render_body
from researchlib.wechat_dialogue import present_ocr_pieces, present_stored_ocr
from researchlib.zsxq_title import zsxq_display_title

# 文章 14886 的形状：正文一句，判断在两张重叠的微信群截图里。
SHOT_A = """9:41
100%
5G
微信
瑞幸古茗交流群(86)
方伟Rey
瑞幸和古茗都算头部，但瑞幸同店还有压力
姜禄彦
古茗店效还行，瑞幸的折扣不能再深了
设为群待办
¥9.9
小程序
瑞幸咖啡
徐哲航
先看到这里，不展开了
"""
SHOT_B = """20:41
微信
87%
瑞幸古茗交流群(86)
姜禄彦
古茗店效还行，瑞幸的折扣不能再深了
设为群待办
徐哲航
先看到这里，不展开了
方伟
同店压力还在，折扣不能再深
Rey
会员那边先不动
"""
EXPECTED = (
    "方伟：瑞幸和古茗都算头部，但瑞幸同店还有压力。\n"
    "\n"
    "网友：古茗店效还行，瑞幸的折扣不能再深了\n"
    "先看到这里，不展开了。\n"
    "\n"
    "方伟：同店压力还在，折扣不能再深\n"
    "会员那边先不动。"
)


def test_14886_screenshot_becomes_one_dialogue():
    text = present_ocr_pieces([SHOT_A, SHOT_B])
    assert text == EXPECTED
    assert present_stored_ocr(text) == text
    # 库里若还是两张图按空行拼起来的原始识别，读的时候也得到同一份。
    assert present_stored_ocr(SHOT_A.strip() + "\n\n" + SHOT_B.strip()) == text
    assert compose_ocr_text([SHOT_A, "logo", SHOT_B]) == text


def test_speaker_rules_and_noise_are_gone():
    text = present_ocr_pieces([SHOT_A, SHOT_B])
    assert text.count("方伟：") == 2
    assert text.count("网友：") == 1
    assert "姜禄彦" not in text and "徐哲航" not in text and "Rey" not in text
    for noise in ("设为群待办", "小程序", "¥9.9", "瑞幸咖啡", "交流群", "9:41", "20:41", "100%", "5G", "微信"):
        assert noise not in text
    # 跨图重复的句子只留一次。连续的网友合成一块，方伟后来又说的话另起一块。
    assert text.count("古茗店效还行，瑞幸的折扣不能再深了") == 1
    assert text.count("先看到这里，不展开了") == 1
    assert text.index("先看到这里") < text.index("同店压力还在")


def test_unclear_speaker_is_a_reader_not_fangwei():
    raw = "微信\n9:41\n方伟说得对，同店还有压力，先观望一下\n"
    text = present_ocr_pieces([raw])
    assert text.startswith("网友：")
    assert "方伟：" not in text
    assert "方伟说得对，同店还有压力，先观望一下" in text


def test_host_aliases_and_inline_names():
    raw = "方伟（星主）\n会员那边先不动\n姜禄彦：古茗店效还行，折扣不能再深了\n"
    text = present_ocr_pieces([raw])
    assert text.startswith("方伟：会员那边先不动")
    assert "网友：古茗店效还行，折扣不能再深了" in text
    assert "姜禄彦" not in text and "星主" not in text


def test_consecutive_same_speaker_merges_and_table_stays():
    chat = "方伟Rey\n第一句还在观察同店。\n方伟\n第二句先不提价。\n"
    assert present_ocr_pieces([chat]) == "方伟：第一句还在观察同店。\n第二句先不提价。"
    table = "广告收入  1234 亿元\n主站货币化率见顶"
    assert compose_ocr_text([table, "x"]) == table
    assert "方伟：" not in compose_ocr_text([table])


def test_reading_page_shows_the_dialogue_once():
    first = "ab" * 32
    second = "cd" * 32
    html = str(
        render_body(
            "瑞幸古茗是比较头部的。\n\n![](a.png)\n\n![](b.png)\n",
            {
                "a.png": f"/media/fangwei-zsxq/ab/{first}.png",
                "b.png": f"/media/fangwei-zsxq/cd/{second}.png",
            },
        )
    )
    page = inline_image_ocr(html, {first: SHOT_A, second: SHOT_B})
    assert "瑞幸古茗是比较头部的" in page
    assert 'class="ocr-reading ocr-dialogue"' in page
    assert '<span class="who">方伟：</span>瑞幸和古茗都算头部，但瑞幸同店还有压力' in page
    assert '<span class="who">网友：</span>古茗店效还行，瑞幸的折扣不能再深了' in page
    assert "姜禄彦" not in page and "设为群待办" not in page and "小程序" not in page
    assert page.count("古茗店效还行，瑞幸的折扣不能再深了") == 1
    assert page.count("点击看原图") == 2
    assert f'<figure class="ocr-shot" id="{image_anchor(first)}">' in page
    assert f'<figure class="ocr-shot" id="{image_anchor(second)}">' in page
    assert page.index("方伟：") < page.index("点击看原图")


def test_digest_input_is_the_cleaned_dialogue():
    reading = append_ocr("瑞幸古茗是比较头部的。", compose_ocr_text([SHOT_A, SHOT_B]))
    assert "【图片里的字】" in reading
    assert EXPECTED in reading
    assert "设为群待办" not in reading and "姜禄彦" not in reading


def test_image_post_title_uses_the_sentence_not_the_speaker_label():
    title = zsxq_display_title("图片", body="（见图）\n", ocr=EXPECTED, when="2024-10-19")
    assert title.startswith("瑞幸和古茗都算头部")
    assert not title.startswith("方伟")
    assert "网友" not in title

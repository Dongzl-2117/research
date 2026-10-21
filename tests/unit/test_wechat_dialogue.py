"""微信截图 OCR 整理成「方伟：」「网友：」。

合成样例覆盖说话人、去重和界面字。tests/fixtures/14886_live_shots.txt 是文章 14886
上线后的三张图，按那份真实识别结果验收。
"""

from pathlib import Path

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
    "网友：古茗店效还行，瑞幸的折扣不能再深了。\n"
    "\n"
    "网友：先看到这里，不展开了。\n"
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
    assert text.count("网友：") == 2
    assert "姜禄彦" not in text and "徐哲航" not in text and "Rey" not in text
    for noise in ("设为群待办", "小程序", "¥9.9", "瑞幸咖啡", "交流群", "9:41", "20:41", "100%", "5G", "微信"):
        assert noise not in text
    # 跨图重复的句子只留一次。两位网友各开一块，方伟后来又说的话另起一块。
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
    assert page.count('class="ocr-shot"') == 1
    assert page.count('class="ocr-reading ocr-dialogue"') == 1
    assert f'<a class="ocr-link" id="{image_anchor(first)}"' in page
    assert f'<a class="ocr-link" id="{image_anchor(second)}"' in page
    assert "原图 1" in page and "原图 2" in page
    assert "<details" not in page
    assert page.index("方伟：") < page.index("原图 1")


def test_digest_input_is_the_cleaned_dialogue():
    reading = append_ocr("瑞幸古茗是比较头部的。", compose_ocr_text([SHOT_A, SHOT_B]))
    assert "【图片里的字】" in reading
    assert EXPECTED in reading
    assert "设为群待办" not in reading and "姜禄彦" not in reading


def _live_shots() -> list[str]:
    text = (Path(__file__).resolve().parents[1] / "fixtures" / "14886_live_shots.txt").read_text()
    shots: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("=== shot"):
            if current:
                shots.append("\n".join(current).strip())
            current = []
            continue
        if line.startswith("#"):
            continue
        current.append(line)
    if current:
        shots.append("\n".join(current).strip())
    return [shot for shot in shots if shot]


def test_live_14886_shots_drop_chrome_and_guest_names():
    """线上三张图：价格粘在商品上、小小程序、@备注、短回复前的名字都不能漏进对话。"""
    shots = _live_shots()
    assert len(shots) == 3
    text = present_ocr_pieces(shots)
    assert present_stored_ocr(text) == text
    for leaked in (
        "姜禄彦",
        "徐哲航",
        "@think",
        "￥",
        "¥",
        "小小程序",
        "小程序",
        "设为群待办",
        "美团外卖",
        "看十年价投",
        "10:38",
        "09:57",
        "10:03",
        "10:18",
        "QuestMobile",
        "泰式风味红茶",
        "黑糖珍珠牛乳",
        "劝学荟",
        "玉鸟集",
        "方伟：",
    ):
        assert leaked not in text, leaked
    assert "网友：怎么定义古茗做咖啡成功了呢" in text
    assert "网友：数据确实是好的" in text
    assert "网友：并且高密度的门店区域" in text
    assert text.count("成功的也不多，就一个古茗") == 1
    assert text.count("要么需求侧少喝咖啡") == 1
    assert "的" in text.splitlines()


def _shots_14879() -> list[str]:
    text = (Path(__file__).resolve().parents[1] / "fixtures" / "14879_live_shots.txt").read_text()
    shots: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "====":
            if current:
                shots.append("\n".join(current[2:]).strip())
            current = []
            continue
        current.append(line)
    if current:
        shots.append("\n".join(current[2:]).strip())
    return [shot for shot in shots if shot]


def test_live_14879_strips_dylan_and_splits_turns():
    """Dylan 是网友昵称。@方伟写在句子里，不能因此编出「方伟：」。"""
    shots = _shots_14879()
    assert len(shots) == 7
    text = present_ocr_pieces(shots)
    assert text.startswith("网友：")
    assert "网友：Dylan" not in text
    assert "Dylan" not in text
    assert "方伟：" not in text
    assert text.count("网友：") >= 3
    for leaked in ("10:57", "10:43", "10:49", "看十年", "撤回了一条消息"):
        assert leaked not in text, leaked
    assert "@方伟-最近在" in text


def test_image_post_title_uses_the_sentence_not_the_speaker_label():
    title = zsxq_display_title("图片", body="（见图）\n", ocr=EXPECTED, when="2024-10-19")
    assert title.startswith("瑞幸和古茗都算头部")
    assert not title.startswith("方伟")
    assert "网友" not in title

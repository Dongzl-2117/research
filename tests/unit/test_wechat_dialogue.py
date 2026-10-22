"""微信截图 OCR 整理成「方伟：」「网友：」。

合成样例覆盖说话人、去重和界面字。tests/fixtures/14886_live_shots.txt 是文章 14886
上线后的三张图，按那份真实识别结果验收。
"""

from pathlib import Path

from researchlib.ocr import append_ocr, compose_ocr_text
from researchlib.render import (
    _dialogue_html,
    _flow_ocr_lines,
    flow_ocr_quote_body,
    image_anchor,
    inline_image_ocr,
    render_body,
)
from researchlib.wechat_dialogue import parse_shot, present_ocr_pieces, present_stored_ocr, render_turns
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


def test_guest_words_about_fangwei_stay_with_the_guest():
    """句子里出现方伟，只要上面是别人的昵称，仍是灰气泡。"""
    raw = "微信\n9:41\nDylan\n方伟说得对，同店还有压力，先观望一下\n"
    text = present_ocr_pieces([raw])
    assert text.startswith("网友：")
    assert "方伟：" not in text
    assert "方伟说得对，同店还有压力，先观望一下" in text


def test_unnamed_bubble_in_a_group_chat_is_the_host():
    """作者自己的绿色气泡不写名字。没有坐标时，不带别人昵称的气泡是方伟。"""
    raw = "微信\n9:41\n同店还有压力，先观望一下\n"
    text = present_ocr_pieces([raw])
    assert text.startswith("方伟：")
    assert "网友：" not in text


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
    ):
        assert leaked not in text, leaked
    # 开头没有别人的昵称，是作者自己的绿色气泡。姜禄彦、徐哲航仍是网友。
    assert text.startswith("方伟：同样是捕抓")
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
    """Dylan 是网友昵称。句子里的 @方伟 不另起「方伟：」，显示名「方伟-最近在杭州：」才是。"""
    shots = _shots_14879()
    assert len(shots) == 7
    text = present_ocr_pieces(shots)
    assert text.startswith("网友：")
    assert "网友：Dylan" not in text
    assert "Dylan" not in text
    assert text.count("网友：") >= 3
    assert text.count("方伟：") >= 3
    assert "方伟：会有一批大模型达到" in text
    assert "方伟-最近在杭州" not in text
    for leaked in ("10:57", "10:43", "10:49", "看十年", "撤回了一条消息"):
        assert leaked not in text, leaked
    assert "@方伟-最近在" in text
    blocks = text.split("\n\n")
    question = next(block for block in blocks if "@方伟-最近在" in block)
    assert "Opus5.5" not in question
    assert any(block.startswith("方伟：Opus5.5确实断档领先") for block in blocks)
    assert any("但大模型的用户体验评价维度" in block and block.startswith("方伟：") for block in blocks)
    host = next(block for block in blocks if "会有一批大模型达到" in block and block.startswith("方伟："))
    assert host.startswith("方伟：会有一批大模型达到")


def test_host_status_suffix_on_shot_23820_is_a_fangwei_turn():
    """23820 的显示名带着「-最近在杭州」，标签只写「方伟：」，后缀不留在正文。"""
    raw = (
        "10:50\n"
        "Dylan\n"
        "这个我认可。\n"
        "方伟-最近在杭州：会有一批大模型达到\n"
        "Opus5.5当前的智能，且只有十分之一的...\n"
        "希望有但不可知\n"
        "差异化一般而言应该是应用层的事情\n"
    )
    text = present_ocr_pieces([raw])
    assert "方伟-最近在杭州" not in text
    assert (
        "方伟：会有一批大模型达到\n"
        "Opus5.5当前的智能，且只有十分之一的...\n"
        "希望有但不可知\n"
        "差异化一般而言应该是应用层的事情。"
    ) in text
    assert present_ocr_pieces(["Dylan\n方伟说得对，同店还有压力，先观望一下\n微信\n"]).startswith("网友：")


def test_screenshot_wraps_flow_into_one_paragraph():
    """满行的折行接上，短行不把两句粘死。旁路文本仍保留换行，这里只测阅读页用的整理。"""
    assert _flow_ocr_lines("最近Claude被封号了，用了一些国\n产agents，觉得差距太大了") == (
        "最近Claude被封号了，用了一些国产agents，觉得差距太大了"
    )
    assert _flow_ocr_lines("Opus5.5确实断档领先其他所有大模\n型。") == "Opus5.5确实断档领先其他所有大模型。"
    assert _flow_ocr_lines("机或者游戏潮玩\n他是一个效率驱动的东西") == "机或者游戏潮玩他是一个效率驱动的东西"
    assert "\n" not in _flow_ocr_lines("同店压力还在，折扣不能再深\n会员那边先不动。")


def test_live_14879_reading_page_flows_wraps_and_keeps_turns():
    """14879 的硬换行是截图宽度，不是分段。阅读页收成一段，说话人和原图链接还在。"""
    shots = _shots_14879()
    parts = ["（见图）"]
    mapping: dict[str, str] = {}
    texts: dict[str, str] = {}
    for index, shot in enumerate(shots, start=1):
        digest = f"{index:02d}" * 32
        name = f"s{index}.png"
        mapping[name] = f"/media/fangwei-zsxq/ab/{digest}.png"
        texts[digest] = shot
        parts.append(f"![]({name})")
    page = inline_image_ocr(str(render_body("\n\n".join(parts) + "\n", mapping)), texts)
    assert "国产agents" in page
    assert "国\n产" not in page
    assert "根本不是一个代际的产品" in page
    assert "其他所有大模型" in page
    assert "智能手机或者游戏潮玩" in page
    assert "游戏潮玩他是一个效率驱动" in page
    assert "agents执行复杂任务" in page
    assert '<span class="who">方伟：</span>会有一批大模型达到' in page
    assert page.count('<span class="who">方伟：</span>') >= 3
    assert page.count('<span class="who">网友：</span>') >= 3
    assert page.count('class="ocr-reading ocr-dialogue"') == 1
    assert "Dylan" not in page
    # 显示名只用于认出方伟，不写成说话人。句子里的 @ 折行后仍是一整句。
    assert "方伟-最近在杭州：" not in page
    assert "@方伟-最近在杭州" in page
    for number in range(1, len(shots) + 1):
        assert f"原图 {number}" in page
    assert page.index("方伟：") < page.index("原图 1")
    # 旁路和检索仍用带换行的整理结果，不因阅读页折行而改掉。
    stored = present_ocr_pieces(shots)
    assert "用了一些国\n产agents" in stored


def test_image_post_title_uses_the_sentence_not_the_speaker_label():
    title = zsxq_display_title("图片", body="（见图）\n", ocr=EXPECTED, when="2024-10-19")
    assert title.startswith("瑞幸和古茗都算头部")
    assert not title.startswith("方伟")
    assert "网友" not in title


PDD_MOMENTS = """拼多多，我是始终不太看好。属于
假本分。个人一家之言仅供参.
方伟看十年作者
零售的本分是帮消费者省钱，
管理层的本分是integrity，世
界范围内在零售行业，做得比
它好的，其实只有Costco，奥
乐齐，利德。
50年下来，沃尔玛的毛利率从
20%提升到了24%，费用率从
16%提升到了20%，只有
Costco50年不变，费用率纹丝
不动，那就是9%，这是零售
领域最强大的公司
方伟看十年
什么才叫做“拥有一家公司”的感觉？"""

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


def test_moments_card_becomes_fangwei_dialogue_without_chrome():
    """8881 拼多多微信图：收成方伟气泡，去掉账号栏和下一帖标题，折行接上。"""
    shot = parse_shot(PDD_MOMENTS)
    assert shot.kind == "dialogue"
    text = render_turns(shot.turns)
    assert text.startswith("方伟：")
    assert "拼多多，我是始终不太看好" in text
    assert "属于" in text and "假本分" in text
    assert "方伟看十年" not in text
    assert "什么才叫做" not in text
    assert "网友：" not in text

    digest = "29" * 32
    page = inline_image_ocr(
        str(render_body("（见图）\n\n![](p.png)\n", {"p.png": f"/media/x/ab/{digest}.png"})),
        {digest: PDD_MOMENTS},
    )
    assert 'class="ocr-reading ocr-dialogue"' in page
    assert '<span class="who">方伟：</span>' in page
    assert "原图 1" in page
    assert "点击看原图" not in page
    assert "<details" not in page
    assert f'id="{image_anchor(digest)}"' in page


def test_product_and_chart_shots_are_links_only():
    """亚马逊 / Temu 商品图和图表不套对话，只留原图链接。"""
    assert parse_shot(AMAZON_PRODUCT).kind == "media"
    assert parse_shot(META_CHART).kind == "media"
    a, c = "e5" * 32, "ad" * 32
    page = inline_image_ocr(
        str(
            render_body(
                "![](a.png)\n\n![](c.png)\n",
                {
                    "a.png": f"/media/x/ab/{a}.png",
                    "c.png": f"/media/x/ab/{c}.png",
                },
            )
        ),
        {a: AMAZON_PRODUCT, c: META_CHART},
    )
    assert "方伟：" not in page and "网友：" not in page
    assert "运动水杯" not in page and "Sponsored" not in page
    assert "Builtwith" not in page
    assert "原图 1" in page and "原图 2" in page
    assert "点击看原图" not in page
    assert page.count('class="ocr-shot"') == 1



def test_cjk_wrap_does_not_insert_space():
    assert _flow_ocr_lines("管理层的本分是integrity，世\n界范围内在零售行业") == (
        "管理层的本分是integrity，世界范围内在零售行业"
    )
    assert _flow_ocr_lines("其实只有Costco，奥\n乐齐，利德。") == "其实只有Costco，奥乐齐，利德。"
    assert "奥 乐齐" not in flow_ocr_quote_body("方伟：其实只有Costco，奥\n乐齐，利德。")


def test_sentence_end_starts_new_dialogue_paragraph():
    """已经收了句的下一句另起一段，避免星球长聊糊成一大块。"""
    assert _flow_ocr_lines("第一句完了。\n第二句开始。") == "第一句完了。\n\n第二句开始。"
    html = _dialogue_html("方伟：第一句完了。\n第二句开始。")
    assert html.count("<p>") == 2
    assert '<span class="who">方伟：</span>第一句完了。' in html
    assert "<p>第二句开始。</p>" in html

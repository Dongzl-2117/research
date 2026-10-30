"""微信截图 OCR 整理成「方伟：」「网友：」。

合成样例覆盖说话人、去重和界面字。tests/fixtures/14886_live_shots.txt 是文章 14886
上线后的三张图的识别结果，按它验收。样本里群友的昵称、群名和店名换成了虚构的，其余照原样。
"""

from pathlib import Path

from researchlib.ocr import append_ocr, compose_ocr_text
from researchlib.render import (
    _dialogue_html,
    _flow_ocr_lines,
    _hoist_figures_out_of_paragraphs,
    flow_ocr_quote_body,
    image_anchor,
    inline_image_ocr,
    render_body,
)
from researchlib.wechat_dialogue import (
    SIDE_SELF,
    parse_shot,
    present_ocr_pieces,
    present_stored_ocr,
    render_turns,
)
from researchlib.zsxq_title import zsxq_display_title

# 文章 14886 的形状：正文一句，判断在两张重叠的微信群截图里。
SHOT_A = """9:41
100%
5G
微信
瑞幸古茗交流群(86)
方伟Rey
瑞幸和古茗都算头部，但瑞幸同店还有压力
王小明
古茗店效还行，瑞幸的折扣不能再深了
设为群待办
¥9.9
小程序
瑞幸咖啡
李小红
先看到这里，不展开了
"""
SHOT_B = """20:41
微信
87%
瑞幸古茗交流群(86)
王小明
古茗店效还行，瑞幸的折扣不能再深了
设为群待办
李小红
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
    assert "王小明" not in text and "李小红" not in text and "Rey" not in text
    for noise in ("设为群待办", "小程序", "¥9.9", "瑞幸咖啡", "交流群", "9:41", "20:41", "100%", "5G", "微信"):
        assert noise not in text
    # 跨图重复的句子只留一次。两位网友各开一块，方伟后来又说的话另起一块。
    assert text.count("古茗店效还行，瑞幸的折扣不能再深了") == 1
    assert text.count("先看到这里，不展开了") == 1
    assert text.index("先看到这里") < text.index("同店压力还在")


def test_guest_words_about_fangwei_stay_with_the_guest():
    """句子里出现方伟，只要上面是别人的昵称，仍是灰气泡。"""
    raw = "微信\n9:41\nKevin\n方伟说得对，同店还有压力，先观望一下\n"
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
    raw = "方伟（星主）\n会员那边先不动\n王小明：古茗店效还行，折扣不能再深了\n"
    text = present_ocr_pieces([raw])
    assert text.startswith("方伟：会员那边先不动")
    assert "网友：古茗店效还行，折扣不能再深了" in text
    assert "王小明" not in text and "星主" not in text


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
    assert "王小明" not in page and "设为群待办" not in page and "小程序" not in page
    assert page.count("古茗店效还行，瑞幸的折扣不能再深了") == 1
    assert page.count('class="ocr-shot"') == 1
    assert page.count('class="ocr-reading ocr-dialogue"') == 1
    assert f'<a class="ocr-link" id="{image_anchor(first)}"' in page
    assert f'<a class="ocr-link" id="{image_anchor(second)}"' in page
    assert "原图 1" not in page and "原图 2" not in page
    assert "<details" not in page
    first_at = page.index(f'id="{image_anchor(first)}"')
    assert first_at < page.index("瑞幸和古茗都算头部", first_at) < page.index("</a>", first_at)
    second_at = page.index(f'id="{image_anchor(second)}"')
    assert second_at < page.index("同店压力还在", second_at) < page.index("</a>", second_at)


def test_digest_input_is_the_cleaned_dialogue():
    reading = append_ocr("瑞幸古茗是比较头部的。", compose_ocr_text([SHOT_A, SHOT_B]))
    assert "【图片里的字】" in reading
    assert EXPECTED in reading
    assert "设为群待办" not in reading and "王小明" not in reading


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
        "王小明",
        "李小红",
        "@think",
        "￥",
        "¥",
        "小小程序",
        "小程序",
        "设为群待办",
        "美团外卖",
        "示例价投",
        "10:38",
        "09:57",
        "10:03",
        "10:18",
        "QuestMobile",
        "泰式风味红茶",
        "黑糖珍珠牛乳",
        "文创园",
        "万象城",
    ):
        assert leaked not in text, leaked
    # 开头没有别人的昵称，是作者自己的绿色气泡。王小明、李小红仍是网友。
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


def test_live_14879_strips_the_guest_nickname_and_splits_turns():
    """Kevin 是网友昵称。句子里的 @方伟 不另起「方伟：」，显示名「方伟-最近在杭州：」才是。"""
    shots = _shots_14879()
    assert len(shots) == 7
    text = present_ocr_pieces(shots)
    assert text.startswith("网友：")
    assert "网友：Kevin" not in text
    assert "Kevin" not in text
    assert text.count("网友：") >= 3
    assert text.count("方伟：") >= 3
    assert "方伟：会有一批大模型达到" in text
    assert "方伟-最近在杭州" not in text
    for leaked in ("10:57", "10:43", "10:49", "示例", "撤回了一条消息"):
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
        "Kevin\n"
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
    assert present_ocr_pieces(["Kevin\n方伟说得对，同店还有压力，先观望一下\n微信\n"]).startswith("网友：")


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
    assert "Kevin" not in page
    # 显示名只用于认出方伟，不写成说话人。句子里的 @ 折行后仍是一整句。
    assert "方伟-最近在杭州：" not in page
    assert "@方伟-最近在杭州" in page
    for number in range(1, len(shots) + 1):
        digest = f"{number:02d}" * 32
        assert f'id="{image_anchor(digest)}"' in page
        assert f"/media/fangwei-zsxq/ab/{digest}.png" in page
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
领域最强大的公司。
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


PDD_MOMENTS_DIALOGUE = """网友：拼多多，我是始终不太看好。属于
假本分。个人一家之言仅供参.

方伟：零售的本分是帮消费者省钱，
管理层的本分是integrity，世
界范围内在零售行业，做得比
它好的，其实只有Costco，奥
乐齐，利德。
50年下来，沃尔玛的毛利率从
20%提升到了24%，费用率从
16%提升到了20%，只有
Costco50年不变，费用率纹丝
不动，那就是9%，这是零售
领域最强大的公司。"""


def test_quote_reply_card_puts_the_guest_above_the_account_bar():
    """8881 拼多多引用回复：账号栏以上是网友，栏下才是方伟。账号栏和下一帖标题去掉。"""
    shot = parse_shot(PDD_MOMENTS)
    assert shot.kind == "dialogue"
    assert shot.family == "moments"
    text = present_ocr_pieces([PDD_MOMENTS])
    assert text == render_turns(shot.turns) == PDD_MOMENTS_DIALOGUE
    assert present_stored_ocr(text) == text
    guest, host = text.split("\n\n", 1)
    assert guest.startswith("网友：") and "不太看好" in guest and "假本分" in guest
    assert host.startswith("方伟：") and "零售的本分" in host
    assert "不太看好" not in host and "拼多多" not in host
    assert "方伟看十年" not in text and "什么才叫做" not in text

    digest = "29" * 32
    page = inline_image_ocr(
        str(render_body("（见图）\n\n![](p.png)\n", {"p.png": f"/media/x/ab/{digest}.png"})),
        {digest: PDD_MOMENTS},
    )
    assert 'class="ocr-reading ocr-dialogue"' in page
    assert '<span class="who">网友：</span>' in page and '<span class="who">方伟：</span>' in page
    guest_html, host_html = page.split('<span class="who">方伟：</span>', 1)
    assert "不太看好" in guest_html and "假本分" in guest_html
    assert "零售的本分" in host_html and "不太看好" not in host_html
    assert "方伟看十年" not in page and "什么才叫做" not in page
    assert f'<a class="ocr-link" id="{image_anchor(digest)}"' in page
    assert "原图 1" not in page
    assert "点击看原图" not in page
    assert "<details" not in page
    assert f'id="{image_anchor(digest)}"' in page
    assert page.index("网友：") < page.index("不太看好") < page.index("方伟：") < page.index("零售的本分")


def test_account_bar_is_the_quote_reply_divider_not_a_bubble():
    """栏上没有引用时仍整段是方伟。只有「方伟看十年」、或靠右的侧标记，也不把引用收成方伟。"""
    badge_first = (
        "方伟看十年作者\n"
        "零售的本分是帮消费者省钱。\n"
        "方伟看十年\n"
        "什么才叫做“拥有一家公司”的感觉？"
    )
    own_post = present_ocr_pieces([badge_first])
    assert own_post == "方伟：零售的本分是帮消费者省钱。"
    assert "网友：" not in own_post and "方伟看十年" not in own_post and "什么才叫做" not in own_post

    name_only = "拼多多，我是始终不太看好。\n方伟看十年\n零售的本分是帮消费者省钱。"
    split = present_ocr_pieces([name_only])
    assert split.startswith("网友：拼多多，我是始终不太看好。")
    assert "方伟：零售的本分是帮消费者省钱。" in split
    assert "方伟看十年" not in split

    sided = (
        f"{SIDE_SELF}拼多多，我是始终不太看好。属于假本分。\n"
        "方伟看十年作者\n"
        "零售的本分是帮消费者省钱。"
    )
    marked = present_ocr_pieces([sided])
    assert marked.startswith("网友：拼多多，我是始终不太看好。属于假本分。")
    assert "方伟：零售的本分是帮消费者省钱。" in marked


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

REDDIT_COMMENT = """Templar-kun.3moago
I agree that for the same price, products from Temu will be a higher quality.
Share
Reply
Award
"""

REDDIT_COMMENT_2 = """Dazzling_Lion7140.3moago
I love Temu, you can find anything....l mean they will refund you if you're
unhappy so its more than worth any risk and the shipping times are amazing
now for most items
"""

SCF_TABLE = """美联储《消费者金融调查》（SCF）2022年的数据：
指标  数值
家庭净资产中位数  约19.3万美元
净资产≥100万美元的家庭  约18%
前10%的门槛  约192万美元
前5%的门槛  约378万美元
净资产≥1.000万美元的家庭  约2%（第98百分位左右，第三方根据微观数据插值估算）
前1%的门槛  约1.367万美元
"""

GROUP_RETAIL = """21:25
1628  示例·线下深度交流（80）
09:20
美国的零售历史是从梅西百货，
A&P，西尔斯，再到1960年代的折
扣零售商，当时的竞争主要是凯马特
与沃尔玛
"""


def test_reddit_and_table_shots_are_links_only():
    """Reddit 评论楼和 SCF 数据表只留原图，不套方伟/网友。"""
    assert parse_shot(REDDIT_COMMENT).kind == "media"
    assert parse_shot(REDDIT_COMMENT_2).kind == "media"
    assert parse_shot(SCF_TABLE).kind == "media"
    digest = "01" * 32
    page = inline_image_ocr(
        str(render_body("![](r.png)\n", {"r.png": f"/media/x/ab/{digest}.png"})),
        {digest: REDDIT_COMMENT},
    )
    assert "方伟：" not in page and "网友：" not in page
    assert "Templar" not in page and "I agree" not in page
    assert "原图 1" in page


def test_moments_then_group_not_one_blob():
    """朋友圈与群聊不同 family，不糊成「公司美国的零售」。"""
    truncated = PDD_MOMENTS.replace(
        "什么才叫做“拥有一家公司”的感觉？",
        "什么才叫做",
    )
    assert parse_shot(truncated).family == "moments"
    assert parse_shot(GROUP_RETAIL).family == "group"
    assert "什么才叫做" not in render_turns(parse_shot(truncated).turns)
    text = present_ocr_pieces([truncated, GROUP_RETAIL])
    assert text.count("方伟：") == 2
    assert text.count("网友：") == 1
    guest = next(block for block in text.split("\n\n") if "不太看好" in block)
    assert guest.startswith("网友：")
    assert "公司美国的零售" not in text.replace("\n", "")
    assert "最强大的公司" in text
    assert "美国的零售历史是从梅西百货" in text
    a, b = "29" * 32, "2b" * 32
    page = inline_image_ocr(
        str(
            render_body(
                "![](a.png)\n\n![](b.png)\n",
                {
                    "a.png": f"/media/x/ab/{a}.png",
                    "b.png": f"/media/x/ab/{b}.png",
                },
            )
        ),
        {a: truncated, b: GROUP_RETAIL},
    )
    assert page.count('class="ocr-reading ocr-dialogue"') == 2
    assert "公司美国的零售" not in page

# 文章 12681 这一类：星球帖子截图的留言区和赞赏栏，不是引用回复的账号栏。
ZSXQ_POST_12681 = """方伟
茅台批价这一周没动，渠道仍在观望，短期不宜提价。
李四留言
渠道库存还是偏高，先别急着看多。
3条留言
划线人数 18
赞赏
分享
"""

ZSXQ_AUTHOR_BADGE = """方伟看十年
（作者）
零售的本分是帮消费者省钱，管理层要对得起消费者。
3条留言
划线人数 4
赞赏
"""


def test_12681_comment_footer_is_not_spoken_by_fangwei():
    """按钮行删掉。李四的话改成网友，不挂在方伟下面，也不整段截掉。"""
    text = present_ocr_pieces([ZSXQ_POST_12681])
    assert text == (
        "方伟：茅台批价这一周没动，渠道仍在观望，短期不宜提价。\n"
        "\n"
        "网友：渠道库存还是偏高，先别急着看多。"
    )
    assert present_stored_ocr(text) == text
    assert present_stored_ocr(ZSXQ_POST_12681) == text
    for noise in ("李四", "留言", "条留言", "划线", "赞赏", "分享"):
        assert noise not in text
    assert "渠道库存还是偏高" not in text.split("网友：", 1)[0]
    digest = "12" * 32
    page = inline_image_ocr(
        str(render_body("（见图）\n\n![](p.png)\n", {"p.png": f"/media/x/ab/{digest}.png"})),
        {digest: ZSXQ_POST_12681},
    )
    assert '<span class="who">方伟：</span>茅台批价这一周没动' in page
    assert '<span class="who">网友：</span>渠道库存还是偏高' in page
    assert "李四" not in page and "划线" not in page and "赞赏" not in page


def test_author_badge_is_chrome_and_footer_does_not_eat_the_reply():
    """「（作者）」是名片，不是分界；底部按钮删掉，栏下的回复仍是方伟。"""
    text = present_ocr_pieces([ZSXQ_AUTHOR_BADGE])
    assert text == "方伟：零售的本分是帮消费者省钱，管理层要对得起消费者。"
    assert "（作者）" not in text and "作者" not in text
    assert "条留言" not in text and "赞赏" not in text and "网友：" not in text
    paren = "拼多多，我是始终不太看好。\n方伟看十年（作者）\n零售的本分是帮消费者省钱。\n"
    split = present_ocr_pieces([paren])
    assert split.startswith("网友：拼多多，我是始终不太看好。")
    assert "方伟：零售的本分是帮消费者省钱。" in split
    assert "作者" not in split
    # 引用回复的账号栏分界还在，不因为新的页脚规则把栏上收成方伟。
    assert present_ocr_pieces([PDD_MOMENTS]) == PDD_MOMENTS_DIALOGUE


def test_split_name_and_leave_a_message_line_are_both_dropped():
    raw = "方伟\n批价这一周没动，渠道仍在观望。\n李四\n留言\n库存还是偏高。\n写留言\n"
    text = present_ocr_pieces([raw])
    assert text == (
        "方伟：批价这一周没动，渠道仍在观望。\n"
        "\n"
        "网友：库存还是偏高。"
    )
    assert "李四" not in text and "写留言" not in text
    assert "库存还是偏高" not in text.split("网友：", 1)[0]


# 12681 上线后的粘连页脚。473、606 是公众号，同一类按钮粘在一行里。
STICKY_12681 = """方伟
茅台批价这一周没动，渠道仍在观望，短期不宜提价。
分享21 推荐0 划线人数0
赞赏￥0
3条留言V 写留言
写留言
李四留言
渠道库存还是偏高，先别急着看多。
"""
STICKY_EXPECTED = (
    "方伟：茅台批价这一周没动，渠道仍在观望，短期不宜提价。\n"
    "\n"
    "网友：渠道库存还是偏高，先别急着看多。"
)


def test_12681_sticky_footer_is_not_spoken_by_fangwei():
    """粘连按钮只删那一行。按钮后面的李四仍在，页面不能只剩第一句。"""
    text = present_ocr_pieces([STICKY_12681])
    assert text == STICKY_EXPECTED
    assert present_stored_ocr(STICKY_12681) == text
    assert present_stored_ocr(text) == text
    for noise in ("分享", "推荐", "划线", "赞赏", "留言", "李四", "写留言", "条留言"):
        assert noise not in text
    stored = (
        "方伟：茅台批价这一周没动，渠道仍在观望，短期不宜提价。\n"
        "分享21 推荐0 划线人数0\n赞赏￥0\n3条留言V 写留言\n写留言\n"
        "李四留言\n渠道库存还是偏高，先别急着看多。\n"
    )
    assert present_stored_ocr(stored) == STICKY_EXPECTED
    glued = (
        "方伟\n茅台批价这一周没动，渠道仍在观望，短期不宜提价。分享21 推荐0 划线人数0\n"
        "赞赏￥0\n后面这句还在，不能被按钮截掉。\n"
    )
    glued_text = present_ocr_pieces([glued])
    assert "茅台批价这一周没动" in glued_text
    assert "后面这句还在，不能被按钮截掉" in glued_text
    assert "分享21" not in glued_text and "赞赏" not in glued_text
    assert glued_text.index("茅台批价") < glued_text.index("后面这句还在")


def test_12681_dialogue_after_sticky_chrome_is_kept():
    """页脚夹在中间时，后面的方伟和李四都要留下。"""
    raw = """方伟
1月份的腾讯都没买的话，
后面这句也不能丢。
分享21 推荐0 划线人数0
赞赏￥0
3条留言V 写留言
写留言
李四留言
腾讯的位置还在，可以分批。
方伟
分批就行，别一次打满。
"""
    text = present_ocr_pieces([raw])
    assert text == (
        "方伟：1月份的腾讯都没买的话，\n后面这句也不能丢。\n"
        "\n"
        "网友：腾讯的位置还在，可以分批。\n"
        "\n"
        "方伟：分批就行，别一次打满。"
    )
    assert present_stored_ocr(text) == text
    for noise in ("分享21", "推荐0", "划线人数", "赞赏", "写留言", "条留言", "李四"):
        assert noise not in text
    assert text.count("方伟：") == 2
    assert "1月份的腾讯都没买的话" in text
    assert "分批就行" in text


# 12681 回写之后的呈现原文。留言头粘在「方伟：」后面，名字里夹着拉丁字母。
PROD_12681 = """方伟：1月份的腾讯都没买的话，。
网友：感谢，看了你那篇文章，360抄底了腾讯…
方伟：孙小平SunPing留言1次·关注3个月
非常认可
22:07上海
UI留言17次·新的关注
为什么看好微信的视频号业务…
"""


def test_12681_production_fragment_keeps_guest_turns():
    """按线上这一段原文：孙小平/UI 的话归网友，时间地点和留言头不进方伟。"""
    text = present_stored_ocr(PROD_12681)
    assert present_stored_ocr(text) == text
    for block in text.split("\n\n"):
        if block.startswith("方伟："):
            for noise in (
                "孙小平SunPing",
                "孙小平",
                "SunPing",
                "UI留言",
                "22:07上海",
                "非常认可",
                "视频号",
            ):
                assert noise not in block
    assert "1月份的腾讯都没买的话" in text
    assert "感谢，看了你那篇文章，360抄底了腾讯" in text
    guests = [block for block in text.split("\n\n") if block.startswith("网友：")]
    assert any("非常认可" in block for block in guests)
    assert any("为什么看好微信的视频号业务" in block for block in guests)
    timed = "方伟\n会议定在22:07上海办公室见，先别改期。\n"
    assert "22:07上海办公室见" in present_ocr_pieces([timed])


def test_606_friend_follow_chrome_is_dropped():
    """公众号「+关注」「14个朋友关注 3 19。」只删这一行。"""
    raw = """方伟
正文这一句要留下。
+关注
14个朋友关注 3 19。
下一句也要留下。
网友：这句网友的话要留下。
14个朋友关注
"""
    text = present_ocr_pieces([raw])
    assert "正文这一句要留下" in text
    assert "下一句也要留下" in text
    assert "这句网友的话要留下" in text
    assert "+关注" not in text and "个朋友关注" not in text
    kept = present_ocr_pieces(["方伟\n我有14个朋友关注这件事，先放着。\n"])
    assert "我有14个朋友关注这件事，先放着" in kept


def test_606_account_name_and_reader_counts_are_follow_chrome():
    """公众号预览页：「账号名 +关注」「观察 56个朋友关注 82 455 38 33。」整行丢掉，正文不截断。"""
    raw = """方伟
第一句正文要留下。
饮料快暴 +关注
第二句正文也要留下。
品饮汇 品饮汇观察 +关注
观察 56个朋友关注 82 455 38 33。
第三句正文仍然要留下。
"""
    text = present_ocr_pieces([raw])
    assert text == (
        "方伟：第一句正文要留下。\n第二句正文也要留下。\n第三句正文仍然要留下。"
    )
    for noise in ("+关注", "个朋友关注", "饮料快暴", "品饮汇", "观察"):
        assert noise not in text
    assert present_stored_ocr(text) == text
    assert compose_ocr_text([raw]) == text
    kept = present_ocr_pieces(["方伟\n我有14个朋友关注这件事，先放着。\n"])
    assert "我有14个朋友关注这件事，先放着" in kept


def test_12681_author_reply_badge_makes_green_bubbles_fangwei():
    """「方伟在学习（作者）」是回复头：哈哈哈和关系链两段是方伟，UI 的问句仍是网友。"""
    raw = """22:40
1月份的腾讯都没买的话，
分享21  推荐0  划线人数0
赞赏￥0
留言  赞赏
3条留言V  写留言
李四留言1次·关注8个月
感谢，看了你那篇文章，360抄底了腾讯，但是也是
一样的问题。没有卖掉美团更重仓腾讯。
方伟在学习（作者）
哈哈哈因为我想出美团的价格是200-250，其实当
时距离内在价值大概是30%
22:40
以下为历史留言
孙小平SunPing留言1次·关注3个月
非常认可
22:07上海
UI留言17次·新的关注
为什么看好微信的视频号业务
方伟在学习（作者）  2
关系链是一个超级大杀器，能够带来极大的体验提
升
共2条回复
22:07
"""
    text = present_ocr_pieces([raw])
    blocks = text.split("\n\n")
    haha = next(block for block in blocks if "哈哈哈因为我想出美团" in block)
    chain = next(block for block in blocks if "关系链是一个超级大杀器" in block)
    assert haha.startswith("方伟：")
    assert chain.startswith("方伟：")
    assert any(block.startswith("网友：") and "为什么看好微信的视频号业务" in block for block in blocks)
    assert "方伟在学习" not in text and "（作者）" not in text and "作者" not in text
    assert "网友：为什么看好微信的视频号业务\n关系链" not in text
    assert present_stored_ocr(text) == text


SHOP_CARD = """10:39
Q零食礼包
精选  商品  分类  评价  小店介绍
大家都在搜
微信小店是微信生态内的官方购物产品。
￥5.99  点赞补贴价  去参与
上滑继续看视频
"""


def test_473_wechat_shop_card_is_link_only_and_body_keeps_the_word():
    """微信小店商品卡不套对话；正文句子里的「微信小店」四个字照常留下。"""
    assert parse_shot(SHOP_CARD).kind == "media"
    body = "方伟\n并在小游戏、短剧和微信小店广告主中获得了广泛应用。\n"
    text = present_ocr_pieces([body, SHOP_CARD])
    assert text == "方伟：并在小游戏、短剧和微信小店广告主中获得了广泛应用。"
    assert "精选" not in text and "商品" not in text and "去参与" not in text
    assert text.count("微信小店") == 1
    footer = "方伟\n这一段正文要留下。\n交易规则  71个朋友关注  53  47  23  写留言\n"
    dropped = present_ocr_pieces([footer])
    assert dropped == "方伟：这一段正文要留下。"
    assert "71个朋友关注" not in dropped and "交易规则" not in dropped


def test_weibo_and_group_shots_with_search_or_subsidy_words_are_not_shop_pages():
    """「大家都在搜」「百亿补贴」在微博、小红书、京东外卖群里也有，不能整张当商品卡丢掉。"""
    weibo = """Key_Cheung@Key_Cheung·15h  C
谜底的人自己也承认也参考别人的设计嘛。
大家都在搜：谜底黑胶app9  X
其实和stripepress官网的样子也很像，不过没关系啊。
"""
    assert parse_shot(weibo).kind != "media"
    weibo_text = present_ocr_pieces([weibo])
    assert "谜底黑胶" in weibo_text and "stripepress官网" in weibo_text
    group = """18:01
省团&京东外卖商家运营群（73）
京东外卖——“百亿补贴”最热商家活动来袭！
满8-7
以上活动商家对半承担！
"""
    assert parse_shot(group).kind == "dialogue"
    assert "百亿补贴" in present_ocr_pieces([group])


def test_12681_production_guest_header_is_a_netizen_turn():
    """线上留言头是「李四留言1次·关注8个月」，不是光秃的「李四留言」。"""
    raw = """方伟
1月份的腾讯都没买的话，
后面这句也不能丢。
李四留言1次·关注8个月
腾讯的位置还在，可以分批。
以下为历史留言
孙小平
历史里这句也要留下。
共2条回复
关注8个月
留言1次
方伟
分批就行，别一次打满。
"""
    text = present_ocr_pieces([raw])
    assert text == (
        "方伟：1月份的腾讯都没买的话，\n后面这句也不能丢。\n"
        "\n"
        "网友：腾讯的位置还在，可以分批。\n"
        "\n"
        "网友：历史里这句也要留下。\n"
        "\n"
        "方伟：分批就行，别一次打满。"
    )
    assert present_stored_ocr(text) == text
    for noise in (
        "李四",
        "留言1次",
        "关注8个月",
        "以下为历史留言",
        "孙小平",
        "共2条回复",
        "历史留言",
    ):
        assert noise not in text
    host = text.split("网友：", 1)[0]
    assert "腾讯的位置还在" not in host
    assert "历史里这句" not in host


def test_473_mp_shop_and_city_footers_drop_without_eating_body():
    """公众号页脚：微信小店、关注同城、相关的服务号。夹在两句之间只删页脚。"""
    raw = """方伟
渗透率已经过了最陡的那段。
微信小店
关注同城
相关的服务号
分批买比一次买完更重要。
"""
    text = present_ocr_pieces([raw])
    assert text == (
        "方伟：渗透率已经过了最陡的那段。\n"
        "分批买比一次买完更重要。"
    )
    for noise in ("微信小店", "关注同城", "服务号", "小店"):
        assert noise not in text
    glued = "方伟\n微信小店关注同城\n相关的服务号\n服务号的毛利先不看，看订阅。\n"
    kept = present_ocr_pieces([glued])
    assert "服务号的毛利先不看，看订阅" in kept
    assert "微信小店" not in kept and "关注同城" not in kept
    sentence = "微信\n9:41\n微信小店的毛利还行，先观察一个季度。\n"
    assert "微信小店的毛利还行" in present_ocr_pieces([sentence])
    composite = """方伟
渗透率已经过了最陡的那段。
关注同城 朋友 推荐
同城朋友推荐
分批买比一次买完更重要。
"""
    city = present_ocr_pieces([composite])
    assert city == (
        "方伟：渗透率已经过了最陡的那段。\n"
        "分批买比一次买完更重要。"
    )
    assert "关注同城" not in city and "朋友推荐" not in city and "同城" not in city


def test_rewash_selector_ignores_zanshang_inside_a_sentence():
    from researchlib.zsxq_repair import _chrome_lines

    assert _chrome_lines("这个赞赏的设计并不值钱，用户不会付钱。") is False
    assert _chrome_lines("赞赏\n分享\n") is True
    assert _chrome_lines("3条留言\n划线人数 18\n") is True
    assert _chrome_lines("李四留言\n渠道还高。\n") is True
    assert _chrome_lines("（作者）\n正文还在。\n") is True
    assert _chrome_lines("分享21 推荐0 划线人数0\n赞赏￥0\n3条留言V 写留言\n") is True
    assert _chrome_lines("饮料快暴 +关注\n") is True
    assert _chrome_lines("品饮汇 品饮汇观察 +关注\n") is True
    assert _chrome_lines("观察 56个朋友关注 82 455 38 33。\n") is True
    assert _chrome_lines("交易规则 71个朋友关注 53 47 23 写留言\n") is True
    assert _chrome_lines("我有14个朋友关注这件事，先放着。\n") is False


def test_group_chat_can_still_say_zanshang_in_a_sentence():
    raw = "微信\n9:41\n这个赞赏的设计并不值钱，用户不会为它付钱。\n"
    text = present_ocr_pieces([raw])
    assert text.startswith("方伟：")
    assert "赞赏的设计并不值钱" in text
    recommend = "微信\n9:41\n推荐这只股票，先别追高。\n"
    assert "推荐这只股票，先别追高" in present_ocr_pieces([recommend])


def test_long_note_drops_a_trailing_footer_without_becoming_dialogue():
    raw = (
        "渠道库存和批价要分开看，这一段不是群聊，也没有人在气泡里说话。\n"
        "后面再补一句，说明这是贴在图里的长笔记，不该套上说话人。\n"
        "3条留言\n赞赏\n分享\n"
    )
    text = present_ocr_pieces([raw])
    assert "方伟：" not in text and "网友：" not in text
    assert "渠道库存和批价要分开看" in text
    assert "条留言" not in text and "赞赏" not in text and "分享" not in text


def test_mid_paragraph_figure_is_hoisted():
    """段永平引语中间的图提到段外，半句不再被打断。"""
    sample = (
        '<p id="p-2"><figure class="ocr-shot"><p class="ocr-links">原图 1</p></figure>'
        "他认为可口可乐是人们必喝的。”</p>"
    )
    out = _hoist_figures_out_of_paragraphs(sample)
    assert out.index("<figure") < out.index("他认为")
    assert "<p id=\"p-2\">他认为可口可乐是人们必喝的。”</p>" in out
    sample2 = '<p><figure class="ocr-shot"><div>SCF</div></figure>——段永平</p>'
    out2 = _hoist_figures_out_of_paragraphs(sample2)
    assert out2.startswith("<figure")
    assert "<p>——段永平</p>" in out2


def test_hold_to_text_and_quote_back_chrome_are_stripped():
    raw = (
        "18:52\n"
        "看十年价投交流群(499)\n"
        "方伟\n"
        "我投资想的是底线\n"
        "一年的底线能不能稳定赚100亿的问题\n"
        "Cody\n"
        "应该想一些非共识的。比如下限赚多少钱一年\n"
        "4月6日12:06\n"
        "如果你的决策是建立在今年业绩涨了3倍，明年再三倍\n"
        "回到引用位置\n"
        "那这种高预期，市场也是这么高预期\n"
        "可按住转文字\n"
        "可按住转文字  E\n"
        "Henri\n"
        "の  可按住转文字\n"
        "HYIIV\n"
    )
    text = present_ocr_pieces([raw])
    assert "方伟：" in text
    assert "网友：" in text
    for noise in (
        "可按住转文字",
        "回到引用位置",
        "18:52",
        "4月6日12:06",
        "看十年价投交流",
        "Cody",
        "HYIIV",
        "の",
    ):
        assert noise not in text, noise
    assert "稳定赚100亿" in text


def test_labeled_vision_chrome_is_not_a_speaker():
    """识图已经写成方伟/网友时，界面行仍要剥掉，不能标成说话人。"""
    raw = (
        "方伟：可按住转文字\n"
        "方伟：一年的底线能不能稳定赚100亿\n"
        "网友：继续提问\n"
        "网友：AI搜索\n"
        "雪球\n"
        "LIVE\n"
        "加人会员群\n"
        "网友：非共识的下限要先想清楚\n"
        "Aaron阿远：高坝期不是这样说的\n"
        "Henri：每般利间先放一边\n"
    )
    text = compose_ocr_text([raw])
    assert "方伟：" in text
    assert "网友：" in text
    for noise in (
        "可按住转文字",
        "继续提问",
        "AI搜索",
        "雪球",
        "LIVE",
        "加人会员群",
        "Aaron",
        "Henri",
        "阿远",
    ):
        assert noise not in text, noise
    assert "稳定赚100亿" in text
    assert "非共识的下限" in text
    assert "高坝期不是这样说的" in text
    assert not any(line.startswith("方伟：继续") or line.startswith("网友：AI") for line in text.splitlines())


def test_labeled_chat_keeps_nested_pe_table():
    """识图已标说话人的群聊内嵌估值表，不能当纯表 media 丢掉。"""
    text = """网友：尤其是看到伦敦距离非常近的位子连续开了好几家pop mart
方伟：因为市场还是按130亿利润去算pe
网友：如果如我前述：港股Q3同比-45%~-50%公报出来以后，我估的重点区间应在110-120港元
网友：市值倍数PE 对应股价
网友：16倍 约133港元
网友：14倍 约117港元
网友：12倍 约100港元
网友：10倍 约84港元
方伟：而下半年同比去年几乎必然大幅度下滑的
网友：看看明天市场什么反应
"""
    shot = parse_shot(text)
    assert shot.kind == "dialogue"
    joined = "\n".join(line for turn in shot.turns for line in turn.lines)
    assert "110-120" in joined or "110" in joined
    assert "Q3" in joined or "同比" in joined


def test_zsxq_coupon_card_is_noise():
    raw = """方伟看十年
扫码领取优惠券
仅限前 1000 名
2026/06/15 12:00 至 2027/06/01 12:00
66 元立减金"""
    assert parse_shot(raw).kind == "media"
    assert present_ocr_pieces([raw]) == ""
    labeled = "方伟：扫码领取优惠券\n仅限前 1000 名\n66 元立减金。"
    assert present_ocr_pieces([labeled]) == ""


def test_weibo_comment_labeled_guest():
    raw = """小事7747 山东 2
说实话单日房价超过300的
酒店其中七成以上的客人都
是单位出差，为了多开票只
能在酒店购物而且还死贵。
亚朵酒店靠卖枕头被子半年...
龙视新闻联播"""
    text = present_ocr_pieces([raw])
    assert text.startswith("网友：")
    assert "小事7747" not in text
    assert "龙视新闻联播" not in text
    assert "单日房价" in text


def test_24008_xueqiu_and_video_chrome_stripped():
    """格力公众号插图：视频号关注条、雪球帖头/数字栏、回复@串应剥掉。"""
    video = """领教工坊
INSIDE THE BEST
来源：格力电器董事长方洪波
转型本质就是转人，不换思想就换人。
更多组织管理与领导力洞见，欢迎关注 @领教工坊 视频号
作者提示: 内容为转载
点击 #领教工坊 查看更多
领教工坊  已关注
756  1555  827"""
    text = present_ocr_pieces([video])
    assert "转型本质就是转人" in text
    assert "已关注" not in text
    assert "视频号" not in text
    assert "查看更多" not in text
    assert "作者提示" not in text
    assert "756" not in text

    xueqiu = """清仓格力
超级鹿鼎公
2025-10-31 09:35 · 浙江 · 2022年度十大影响力用户
格力这波我觉得可以再看看基本面。
发讨论
1155  2599  338"""
    text = present_ocr_pieces([xueqiu])
    assert "清仓格力" in text
    assert "基本面" in text
    assert "超级鹿鼎公" not in text
    assert "影响力用户" not in text
    assert "发讨论" not in text
    assert "1155" not in text

    chain = """回复@网友:李录投资也很好啊。//@网友:回复@网友:网友会不会是卖完了再吹？//@网友:看了下今天雪球上的帖子"""
    text = present_ocr_pieces([chain])
    assert "李录投资也很好" in text
    assert text.index("看了下今天雪球上的帖子") < text.index("会不会是卖完了再吹") < text.index("李录投资也很好")
    assert "回复@" not in text
    assert "//@" not in text


# 24010 视频截图：角标、主播名牌、节目水印。新闻标题和数字留下。
HUANGJIU_OCR = """“黄酒一哥” 易主！
浙江绍兴会稽山 首度超越古越龙山
2026年上半年营业收入达8.53亿元
江浙沪的销售额高达70%以上
未成年人请勿饮酒
主播 浩嘉
会稽山超越古越龙山
新经济观察"""

# 23173 理想汽车海报：Logo 和口号是壳，保障条款留下。
LIXIANG_OCR = """理想
连山电池保障计划
双重保障，终身守护。
电池不自燃
终身保障
因原厂电池质量问题导致自燃
车辆达到报废条件
补偿同等配置新车
适用条件:
理想汽车首任车主、非营运车辆，车辆关键零部件按时在官方维保、足额投保车损险、持续开通车联网。
本保障计划的具体内容、适用条件、责任免除及补偿细则，以理想汽车官方渠道公示的条款为准。"""


def test_24010_video_badges_and_23173_ad_shell_are_stripped():
    text = present_ocr_pieces([HUANGJIU_OCR])
    assert "8.53亿" in text
    assert "古越龙山2026" not in text
    assert "黄酒一哥" in text
    assert "会稽山超越古越龙山" in text
    assert "未成年人请勿饮酒" not in text
    assert "主播" not in text
    assert "浩嘉" not in text
    assert "新经济观察" not in text

    poster = present_ocr_pieces([LIXIANG_OCR])
    assert "连山电池保障计划" in poster
    assert "补偿同等配置新车" in poster
    assert "理想汽车" in poster
    assert "适用条件" in poster
    assert "双重保障，终身守护" not in poster
    assert not any(line.strip() == "理想" for line in poster.splitlines())

    # 句子里提到这些词，不是整行角标，留下。
    said = "这位主播把黄酒的利润讲得很清楚，一年到头也就一两亿。"
    assert "主播" in present_ocr_pieces([said])
    notice = "广告里写着未成年人请勿饮酒，但利润讨论还是要单独看。"
    assert "未成年人请勿饮酒" in present_ocr_pieces([notice])

    # 书籍目录不是界面，整段留下。
    toc = "目录\n第一章 商业模式\n第二章 企业文化\n第三章 资本开支还要看回报\n"
    kept = present_ocr_pieces([toc])
    assert "目录" in kept
    assert "第一章" in kept
    assert "资本开支" in kept


def test_video_chrome_is_stripped_on_the_reading_page():
    digest = "ab" * 32
    html = str(render_body("![](shot.png)\n", {"shot.png": f"/media/x/ab/{digest}.png"}))
    page = inline_image_ocr(html, {digest: HUANGJIU_OCR})
    assert "8.53亿" in page
    assert "会稽山超越古越龙山" in page
    assert "未成年人请勿饮酒" not in page
    assert "主播" not in page
    assert "新经济观察" not in page


def test_group_notices_and_status_bar_are_not_anyones_words():
    """群系统提示里是群友的昵称，不是谁说的话。状态栏连着 App 标题，或者粘在第一句前面，也只是界面。"""
    raw = (
        "10:57 :!!! 5GA ■知识星球\n"
        "微信\n"
        "看十年交流群(120)\n"
        "王小明\n"
        '大家觉得组织是护城河么你邀请"李四"加入了群聊撤销 "李四与群里其他人都不是朋友关系，请注意隐私安全"\n'
        "你邀请“张三”、“赵六”加入了群聊\n"
        "撤销\n"
        "以下为新消息\n"
        "方伟Rey\n"
        "组织是支撑，不是护城河本身\n"
    )
    text = present_ocr_pieces([raw])
    leaks = ("李四", "张三", "赵六", "加入了群聊", "隐私安全", "撤销", "以下为新消息", "5GA", "10:57", "知识星球")
    for leaked in leaks:
        assert leaked not in text, leaked
    assert "网友：大家觉得组织是护城河么" in text
    assert "方伟：组织是支撑，不是护城河本身" in text

    glued = present_ocr_pieces(["11:08 :!!! 5GA 式呢，以前觉得汽车行业是很高大上的\n微信\n王小明\n现在呢\n"])
    assert "5GA" not in glued and "11:08" not in glued
    assert "以前觉得汽车行业是很高大上的" in glued

    # 库里已经写成对话的旧旁路，再整理一遍也切掉。
    stored = '方伟：10:57 :!!! 5GA ■知识星球。\n\n网友：大家觉得组织是护城河么你邀请"李四"加入了群聊撤销'
    again = present_stored_ocr(stored)
    assert "李四" not in again and "5GA" not in again and "知识星球" not in again
    assert "大家觉得组织是护城河么" in again


def test_app_logo_line_is_not_anyones_words():
    """状态栏和 App 标题识成两行时，单独一行的「■知识星球」也只是界面，不能标成方伟的话（23946）。"""
    raw = "10:57 :!!! 5GA\n■知识星球\n微信\n看十年交流群(120)\n王小明\n拼多多的护城河是什么呢\n"
    text = present_ocr_pieces([raw])
    assert "知识星球" not in text and "5GA" not in text
    assert "网友：拼多多的护城河是什么呢" in text

    again = present_stored_ocr("方伟：■知识星球。\n\n网友：拼多多的护城河是什么呢。")
    assert "知识星球" not in again
    assert "拼多多的护城河是什么呢" in again

    speech = present_ocr_pieces(["微信\n看十年交流群(120)\n王小明\n这些讨论在哪里能看全？\n方伟Rey\n知识星球里有\n"])
    assert "网友：这些讨论在哪里能看全？" in speech
    assert "方伟：知识星球里有" in speech


def test_invite_words_and_clock_in_speech_stay():
    """话里提到邀请、钟点后面接着汉字，都是正常的话。"""
    speech = "微信\n王小明\n我想邀请大家一起看看这家公司\n方伟Rey\n10:30 开盘以后5G板块一直在跌\n"
    text = present_ocr_pieces([speech])
    assert "我想邀请大家一起看看这家公司" in text
    assert "10:30 开盘以后5G板块一直在跌" in text

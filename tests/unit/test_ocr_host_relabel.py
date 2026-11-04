"""截图里发帖人自己的绿气泡：别的来源写成那个来源的作者名，认群聊也要收紧。

库里、识图结果和检索块里的「方伟：」是固定写法，意思是「截图里发帖人自己」。
乘风破浪、孤独进化体这些来源给读者看、交给模型时换成来源作者名（#101）。
另外，正文里只是提到「微信」「美团外卖」的新闻截图、开头只有一个媒体名的截图，不当群聊。
"""

from researchlib.chunks import ocr_chunks
from researchlib.ocr import append_ocr
from researchlib.render import image_anchor, inline_image_ocr, render_body
from researchlib.wechat_dialogue import (
    canonical_host,
    present_ocr_pieces,
    relabel_host,
    speaker_note,
)

# 编的群聊：状态栏、群名、两个昵称。
CHAT = (
    "微信\n"
    "9:41\n"
    "示例交流群(86)\n"
    "方伟\n"
    "同店压力还在，这次折扣的幅度比上次小很多，渠道那边也有反馈\n"
    "王小明\n"
    "古茗店效还行，瑞幸的折扣不能再深了，渠道反馈说这个价位已经很不舒服了\n"
)


def test_relabel_host_and_canonical_host_round_trip():
    stored = present_ocr_pieces([CHAT])
    assert stored.startswith("方伟：")
    relabelled = relabel_host(stored, "乘风破浪")
    assert relabelled.startswith("乘风破浪：") and "方伟：" not in relabelled
    # 换回去再交给 parse_shot 整理，结果和库里那份一致。
    assert canonical_host(relabelled, "乘风破浪") == stored
    # 方伟自己的来源原样返回。
    assert relabel_host(stored, "方伟") == stored
    assert relabel_host(stored, "") == stored


def test_speaker_note_names_the_source_author():
    stored = present_ocr_pieces([CHAT])
    assert "方伟是星主" in speaker_note(stored)
    note = speaker_note(relabel_host(stored, "乘风破浪"), "乘风破浪")
    assert "这篇的作者乘风破浪" in note and "方伟" not in note
    # 没有对话标记时不补这句。
    assert speaker_note("正文一句，没有截图对话。", "乘风破浪") == ""


def test_reader_page_keeps_the_image_and_models_see_the_source_author():
    stored = present_ocr_pieces([CHAT])
    digest = "ab" * 32
    html = str(render_body("见图。\n\n![](a.png)\n", {"a.png": f"/media/x/ab/{digest}.png"}))
    page = inline_image_ocr(html, {digest: CHAT}, host="乘风破浪")
    assert page == html
    assert f'<img id="{image_anchor(digest)}"' in page
    assert "ocr-reading" not in page and '<span class="who">' not in page
    assert "乘风破浪：" not in page and "同店压力" not in page
    model = append_ocr("见图。", relabel_host(stored, "乘风破浪"))
    assert model.startswith("见图。") and "【图片里的字】" in model
    assert "乘风破浪：" in model and "方伟：" not in model
    assert "同店压力还在" in model


def test_ocr_chunks_relabel_for_other_sources():
    blocks = ocr_chunks([("a" * 64, CHAT)], host="乘风破浪")
    assert blocks and blocks[0][1].startswith("乘风破浪：")
    assert "方伟：" not in blocks[0][1]
    own = ocr_chunks([("a" * 64, CHAT)])
    assert own and own[0][1].startswith("方伟：")


def test_news_screenshot_mentioning_wechat_stays_prose():
    news = (
        "腾讯控股公布三季度业绩\n"
        "微信及WeChat的合并月活跃账户数达到14.1亿\n"
        "游戏业务收入同比增长"
    )
    text = present_ocr_pieces([news])
    assert "方伟：" not in text and "网友：" not in text
    assert "微信及WeChat的合并月活跃账户数达到14.1亿" in text


def test_single_media_name_line_is_not_a_chat():
    media = "财联社\n央行今日开展7000亿元买断式逆回购操作\n市场资金面保持宽松"
    text = present_ocr_pieces([media])
    assert "方伟：" not in text and "网友：" not in text
    assert "央行今日开展7000亿元买断式逆回购操作" in text

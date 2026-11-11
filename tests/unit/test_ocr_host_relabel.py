"""截图里发帖人自己的绿气泡：别的来源写成那个来源的作者名。

库里、识图结果和检索块里的「方伟：」是固定写法，意思是「截图里发帖人自己」。
乘风破浪、孤独进化体这些来源给读者看、交给模型时换成来源作者名（#101）。
"""

from researchlib.chunks import ocr_chunks
from researchlib.ocr import append_ocr, compose_ocr_text, speaker_note
from researchlib.ocr_units import ImageText, normalize_units
from researchlib.render import image_anchor, inline_image_ocr, render_body
from researchlib.wechat_dialogue import relabel_host

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
# 同一张图识图交回的结构：靠右的绿气泡是截图的人自己（self）。
CHAT_UNITS = normalize_units(
    {
        "kind": "chat",
        "blocks": [
            {"type": "message", "self": True, "text": "同店压力还在，这次折扣的幅度比上次小很多，渠道那边也有反馈"},
            {
                "type": "message",
                "speaker": "王小明",
                "text": "古茗店效还行，瑞幸的折扣不能再深了，渠道反馈说这个价位已经很不舒服了",
            },
        ],
    }
)
DIGEST = "ab" * 32


def _stored() -> str:
    return compose_ocr_text([ImageText(DIGEST, CHAT, CHAT_UNITS)])


def test_relabel_host_names_the_source_author():
    stored = _stored()
    assert stored.startswith("方伟：") and "王小明：" in stored
    relabelled = relabel_host(stored, "乘风破浪")
    assert relabelled.startswith("乘风破浪：") and "方伟：" not in relabelled
    assert "王小明：" in relabelled
    # 方伟自己的来源原样返回。
    assert relabel_host(stored, "方伟") == stored
    assert relabel_host(stored, "") == stored


def test_speaker_note_names_the_source_author():
    assert "方伟是星主" in speaker_note()
    note = speaker_note("乘风破浪")
    assert "这篇的作者乘风破浪" in note and "方伟" not in note


def test_reader_page_keeps_the_image_and_models_see_the_source_author():
    stored = _stored()
    html = str(render_body("见图。\n\n![](a.png)\n", {"a.png": f"/media/x/ab/{DIGEST}.png"}))
    page = inline_image_ocr(html, {DIGEST: CHAT}, host="乘风破浪")
    assert page == html
    assert f'<img id="{image_anchor(DIGEST)}"' in page
    assert "ocr-reading" not in page and '<span class="who">' not in page
    assert "乘风破浪：" not in page and "同店压力" not in page
    model = append_ocr("见图。", relabel_host(stored, "乘风破浪"))
    assert model.startswith("见图。") and "【图片里的字】" in model
    assert "乘风破浪：" in model and "方伟：" not in model
    assert "同店压力还在" in model


def test_ocr_chunks_relabel_for_other_sources():
    blocks = ocr_chunks([(DIGEST, CHAT, CHAT_UNITS)], host="乘风破浪")
    assert blocks and blocks[0][1].startswith("乘风破浪：")
    assert "方伟：" not in blocks[0][1]
    own = ocr_chunks([(DIGEST, CHAT, CHAT_UNITS)])
    assert own and own[0][1].startswith("方伟：")

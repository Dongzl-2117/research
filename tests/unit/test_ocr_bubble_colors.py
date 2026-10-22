"""Synthetic RGB rasters test color evidence, not a claimed production screenshot.

The two 12681 fragments are exactly the abbreviated strings supplied in the task;
they are not reconstructed full replies. Merged #55 supplies original 12681 / 473 OCR snippets; those existing tests
are retained. PDD_MOMENTS is the real 8881 text. Screenshots are unavailable.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from researchlib.ocr import OcrUnavailable, compose_ocr_text, order_text, rapid_engine
from researchlib.ocr_bubbles import bubble_color
from researchlib.render import _dialogue_html, flow_ocr_quote_body
from researchlib.wechat_dialogue import (
    BUBBLE_GUEST,
    BUBBLE_SELF,
    BUBBLE_UNKNOWN,
    SIDE_SELF,
    parse_shot,
    present_ocr_pieces,
    present_stored_ocr,
    speaker_note,
)


class Raster:
    """Tiny array protocol so the base/test image needs neither NumPy nor OpenCV."""

    def __init__(self, width=400, height=240, color=(237, 237, 237)):
        self.shape = (height, width, 3)
        self.pixels = [[color] * width for _ in range(height)]

    def __getitem__(self, point):
        y, x = point
        return self.pixels[y][x]

    def rectangle(self, left, top, right, bottom, color):
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                self.pixels[y][x] = color


def item(left, top, right, bottom, text):
    return [[[left, top], [right, top], [right, bottom], [left, bottom]], text, 0.99]


@pytest.mark.parametrize("left", [20, 230])
@pytest.mark.parametrize("color,expected", [((149, 236, 105), "green"), ((255, 255, 255), "gray")])
def test_bubble_color_depends_on_rgb_not_horizontal_side(left, color, expected):
    image = Raster()
    image.rectangle(left, 30, left + 130, 80, color)
    assert bubble_color(image, (left + 10, 40, left + 120, 60)) == expected


@pytest.mark.parametrize("color", [(237, 237, 237), (149, 236, 105), (200, 100, 240), (80, 80, 80)])
def test_uniform_pages_and_unsupported_themes_are_not_bubble_evidence(color):
    image = Raster(color=color)
    assert bubble_color(image, (30, 40, 150, 60)) == ""


def test_colored_glyphs_without_surround_and_mixed_fill_are_unknown():
    image = Raster()
    image.rectangle(30, 40, 150, 60, (149, 236, 105))
    assert bubble_color(image, (30, 40, 150, 60)) == ""
    image.rectangle(20, 30, 160, 80, (149, 236, 105))
    image.rectangle(70, 30, 160, 80, (190, 100, 230))
    assert bubble_color(image, (30, 40, 150, 60)) == ""


@pytest.mark.parametrize("reply", ["哈哈哈…美团", "关系链…"])
def test_supplied_12681_fragment_in_green_overrides_preceding_guest(reply):
    image = Raster()
    # Deliberately put the green bubble on the left, gray on the right.
    image.rectangle(230, 20, 380, 60, (255, 255, 255))
    image.rectangle(20, 90, 170, 140, (149, 236, 105))
    raw = order_text([
        item(240, 30, 370, 45, "这是网友提出的问题"),
        item(30, 100, 160, 120, reply),
    ], rgb_image=image)
    assert raw == BUBBLE_GUEST + "这是网友提出的问题\n" + BUBBLE_SELF + reply
    text = present_ocr_pieces([raw])
    assert text == "网友：这是网友提出的问题。\n\n方伟：" + reply + ("" if reply.endswith("…") else "。")
    assert compose_ocr_text([raw]) == text
    assert present_stored_ocr(text) == text
    assert not any(marker in text for marker in (SIDE_SELF, BUBBLE_SELF, BUBBLE_GUEST, BUBBLE_UNKNOWN))


def test_two_colors_on_same_ocr_row_remain_two_turns():
    image = Raster()
    image.rectangle(20, 20, 170, 70, (149, 236, 105))
    image.rectangle(230, 20, 380, 70, (255, 255, 255))
    text = order_text([
        item(240, 30, 370, 50, "这是网友的回答"),
        item(30, 30, 160, 50, "这是方伟的回答"),
    ], rgb_image=image)
    assert present_ocr_pieces([text]) == "方伟：这是方伟的回答。\n\n网友：这是网友的回答。"


def test_ambiguous_color_does_not_inherit_host_or_guess_from_position():
    text = "\n".join(["微信", BUBBLE_SELF + "这是确认颜色的作者内容", BUBBLE_UNKNOWN + "这段颜色不明不能猜作者"])
    presented = present_ocr_pieces([text])
    assert presented == "方伟：这是确认颜色的作者内容。\n\n未确认：这段颜色不明不能猜作者。"
    assert present_stored_ocr(presented) == presented
    all_unknown = order_text([item(230, 30, 370, 50, "这是一段没有色彩证据的正文")], rgb_image=Raster())
    assert all_unknown.startswith(BUBBLE_UNKNOWN)
    assert present_ocr_pieces([all_unknown]) == "这是一段没有色彩证据的正文"
    assert order_text([item(230, 30, 370, 50, "右侧不代表绿色")]) == "右侧不代表绿色"


def test_explicit_speaker_and_quote_divider_remain_stronger_than_unknown_color():
    text = BUBBLE_UNKNOWN + "方伟\n" + BUBBLE_UNKNOWN + "明确的昵称不需要猜测气泡"
    assert present_ocr_pieces([text]) == "方伟：明确的昵称不需要猜测气泡。"
    # Synthetic account-first layout regression discussed for 14884, NOT its
    # unavailable original text. This must remain the author's own post.
    own_post = BUBBLE_UNKNOWN + "方伟看十年作者\n" + BUBBLE_UNKNOWN + "这条是账号栏下面的正文。"
    assert present_ocr_pieces([own_post]) == "方伟：这条是账号栏下面的正文。"
    quote = BUBBLE_SELF + "栏上是引用，不把绿色标记当作者。\n" + own_post
    assert present_ocr_pieces([quote]) == "网友：栏上是引用，不把绿色标记当作者。\n\n方伟：这条是账号栏下面的正文。"


def test_unknown_metadata_does_not_turn_nonchat_tables_into_dialogue():
    text = "\n".join(BUBBLE_UNKNOWN + line for line in ["公司 财年", "经营利润率 20%", "净利率 15%"])
    assert parse_shot(text).kind == "media"


def test_rapid_engine_reads_original_rgb_pixels(monkeypatch):
    import sys

    image = Raster()
    image.rectangle(20, 20, 170, 70, (149, 236, 105))
    result = [item(30, 30, 160, 50, "哈哈哈…美团")]
    monkeypatch.setitem(
        sys.modules, "rapidocr_onnxruntime", SimpleNamespace(RapidOCR=lambda: lambda path: (result, None))
    )
    calls = []
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(
        imread=lambda path: calls.append(path) or image,
        cvtColor=lambda pixels, mode: pixels,
        COLOR_BGR2RGB=4,
    ))
    assert rapid_engine()(Path("synthetic.png")) == BUBBLE_SELF + "哈哈哈…美团"
    assert calls == ["synthetic.png"]
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(imread=lambda path: None))
    with pytest.raises(OcrUnavailable, match="原图颜色"):
        rapid_engine()(Path("unreadable.png"))


def test_unknown_speaker_stays_visible_in_reader_and_model_input_note():
    text = "未确认：这段颜色无法确认。"
    assert _dialogue_html(text) == '<p><span class="who">未确认：</span>这段颜色无法确认。</p>'
    assert flow_ocr_quote_body(text) == text
    assert "不要当作方伟的判断" in speaker_note(text)
    assert "不要把网友的话写成方伟的判断" in speaker_note("网友：问题。\n\n" + text)


def test_inline_label_does_not_assign_the_next_unknown_bubble_to_host():
    raw = BUBBLE_UNKNOWN + "方伟：第一句作者有标签\n" + BUBBLE_UNKNOWN + "下一气泡没有证据的文字"
    assert present_ocr_pieces([raw]) == "方伟：第一句作者有标签。\n\n未确认：下一气泡没有证据的文字。"


def test_positive_bubble_evidence_protects_genuine_commerce_speech():
    raw = BUBBLE_SELF + "现在美团外卖补贴很多，用户很喜欢。\n" + BUBBLE_SELF + "这句最后的正文要留下。"
    assert present_ocr_pieces([raw]) == "方伟：现在美团外卖补贴很多，用户很喜欢。\n这句最后的正文要留下。"
    assert present_ocr_pieces([BUBBLE_SELF + "微信小店"]) == "方伟：微信小店。"


def test_14884_quote_excerpt_from_merged_pr_55_keeps_roles():
    # Source: https://github.com/Dongzl-2117/research/pull/55 , 8881 / 14884
    # before/after excerpt. This is an excerpt, not the unavailable full image.
    excerpt = (
        "网友：拼多多，我是始终不太看好。属于\n假本分。个人一家之言仅供参.\n\n"
        "方伟：零售的本分是帮消费者省钱，\n管理层的本分是integrity，世"
    )
    text = present_stored_ocr(excerpt)
    guest, host = text.split("\n\n")
    assert guest == "网友：拼多多，我是始终不太看好。属于\n假本分。个人一家之言仅供参."
    assert host.startswith("方伟：零售的本分是帮消费者省钱，\n管理层的本分是integrity，世")
    assert present_stored_ocr(text) == text


def test_explicit_guest_label_ends_the_account_reply_body():
    raw = "\n".join([
        BUBBLE_UNKNOWN + "方伟看十年作者",
        BUBBLE_GUEST + "这条是账号栏下面的正文。",
        BUBBLE_UNKNOWN + "网友：这是网友有标签的发言。",
        BUBBLE_GUEST + "这是同一位网友的后续发言。",
    ])
    text = present_ocr_pieces([raw])
    assert text == (
        "方伟：这条是账号栏下面的正文。\n\n"
        "网友：这是网友有标签的发言。\n这是同一位网友的后续发言。"
    )


@pytest.mark.parametrize("nickname", ["郑立涛", "Dylan"])
def test_uncolored_nickname_before_gray_bubble_is_not_retained(nickname):
    raw = BUBBLE_UNKNOWN + "微信\n" + BUBBLE_UNKNOWN + nickname + "\n" + BUBBLE_GUEST + "这条是网友明确灰色的发言。"
    assert present_ocr_pieces([raw]) == "网友：这条是网友明确灰色的发言。"


def test_unknown_short_utterance_is_not_mistaken_for_a_nickname():
    raw = BUBBLE_UNKNOWN + "微信\n" + BUBBLE_UNKNOWN + "哈哈哈\n" + BUBBLE_GUEST + "这条是网友明确灰色的发言。"
    assert present_ocr_pieces([raw]) == "未确认：哈哈哈。\n\n网友：这条是网友明确灰色的发言。"

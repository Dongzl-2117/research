"""Regression fragments from the task / merged #55, in synthetic surroundings.

#55 supplies the original 12681 reply and 473 shop-card snippets; its tests stay
unchanged. These cases add old-label/line-wrap variants and shared rewash checks.
"""

import pytest

from researchlib.ocr import compose_ocr_text, hash_ocr
from researchlib.wechat_dialogue import (
    BUBBLE_GUEST,
    BUBBLE_HOST,
    BUBBLE_SELF,
    BUBBLE_UNKNOWN,
    parse_shot,
    present_ocr_pieces,
    present_stored_ocr,
)
from researchlib.zsxq_repair import _chrome_lines, _cleaned_ocr, rewash_source_ocr

FOLLOW_ROWS = (
    "饮料快暴 +关注",
    "品饮汇…+关注",
    "71个朋友关注",
    "观察 56个朋友关注 82 455 38 33。",
    "交易规则 71个朋友关注 53 47 23 写留言",
)
BEFORE = "方伟：正文这句必须完整保留。"
AFTER = "网友：后面的评论也必须完整保留。"
EXPECTED = f"{BEFORE}\n\n{AFTER}"


@pytest.mark.parametrize("row", FOLLOW_ROWS)
def test_follow_rows_drop_without_truncating_dialogue(row):
    raw = f"{BEFORE}\n{row}\n{AFTER}"
    assert present_ocr_pieces([raw]) == EXPECTED
    assert present_stored_ocr(raw) == EXPECTED
    assert compose_ocr_text([raw]) == EXPECTED
    assert present_stored_ocr(EXPECTED) == EXPECTED
    assert _chrome_lines(row)
    assert _cleaned_ocr([raw], raw) == EXPECTED
    assert _cleaned_ocr([], raw) == EXPECTED


@pytest.mark.parametrize("label", ["", "方伟：", "网友：", "未确认："])
def test_follow_rows_rewash_even_when_old_output_gave_ui_a_speaker(label):
    raw = f"{BEFORE}\n{label}饮料快暴 +关注\n{AFTER}"
    assert present_stored_ocr(raw) == EXPECTED
    assert _chrome_lines(f"{label}饮料快暴 +关注")


def test_numeric_counter_row_only_drops_immediately_after_follow_ui():
    raw = f"{BEFORE}\n71个朋友关注\n82 455 38 33。\n{AFTER}\n82 455 38 33。"
    text = present_stored_ocr(raw)
    assert "71个朋友关注" not in text
    assert text.count("82 455 38 33。") == 1
    assert text.index(AFTER) < text.index("82 455 38 33。")


@pytest.mark.parametrize("sentence", [
    "我有14个朋友关注这件事，先观察。",
    "我有 56个朋友关注",
    "请点击 +关注",
    "网友提到了 +关注 按钮，不是叫大家关注。",
    "微信小店的毛利还行，先观察一个季度。",
])
def test_ui_words_inside_genuine_speech_are_kept(sentence):
    assert sentence in present_stored_ocr(f"方伟：{sentence}")
    assert not _chrome_lines(sentence)


def test_unknown_color_metadata_does_not_hide_ui_or_counter_rows():
    raw = f"{BEFORE}\n71个朋友关注\n82 455 38 33。\n{AFTER}"
    marked = "\n".join(BUBBLE_UNKNOWN + line for line in raw.splitlines())
    assert present_ocr_pieces([marked]) == EXPECTED


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _RewashConnection:
    """In-memory SQL boundary stub, not a production DB or integration fixture."""

    def __init__(self, source_id, raw, with_images):
        self.source_id = source_id
        self.text = raw
        self.images = [raw] if with_images else []
        self.updates = []

    def execute(self, sql, params):
        query = " ".join(sql.split())
        if query.startswith("SELECT i.article_id, t.text"):
            assert params == (self.source_id,)
            return _Rows([{"article_id": 1, "text": text} for text in self.images])
        if query.startswith("SELECT id, ocr_text FROM articles"):
            assert params == (self.source_id,)
            return _Rows([{"id": 1, "ocr_text": self.text}])
        if query.startswith("UPDATE articles SET ocr_text"):
            text, digest, article_id = params
            assert article_id == 1 and digest == hash_ocr(text)
            self.text = text
            self.updates.append(params)
            return _Rows([])
        raise AssertionError((query, params))


@pytest.mark.parametrize("source_id", ["fangwei-zsxq", "fangwei-mp"])
@pytest.mark.parametrize("with_images", [False, True])
@pytest.mark.parametrize("noise", FOLLOW_ROWS)
def test_repair_rewashes_new_ui_noise_for_both_sources(source_id, with_images, noise):
    conn = _RewashConnection(source_id, f"{BEFORE}\n{noise}\n{AFTER}", with_images)
    assert rewash_source_ocr(conn, source_id) == 1
    assert conn.text == EXPECTED
    assert len(conn.updates) == 1
    assert rewash_source_ocr(conn, source_id) == 0
    assert len(conn.updates) == 1


@pytest.mark.parametrize("marker,speaker", [(BUBBLE_SELF, "未确认"), (BUBBLE_HOST, "方伟"), (BUBBLE_GUEST, "网友")])
def test_follow_counter_cleanup_does_not_eat_confirmed_numeric_speech(marker, speaker):
    raw = f"方伟：正文必须保留。\n71个朋友关注\n{marker}82 455 38 33。\n网友：后面的正文。"
    text = present_ocr_pieces([raw])
    assert "71个朋友关注" not in text
    assert "82 455 38 33。" in text
    numeric_turn = next(turn for turn in parse_shot(raw).turns if "82 455 38 33。" in turn.lines)
    assert numeric_turn.speaker == speaker

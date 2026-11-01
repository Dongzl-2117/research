"""识图误读校对：家电语境里的「墓地」、free cash flow 的单字母滑字。

不进语病表。展示层和识图 normalize 都要接到。
"""

from __future__ import annotations

from researchlib.ocr import normalize_vision_text
from researchlib.present_text import polish_display_text
from researchlib.textutil import fix_obvious_typos
from researchlib.vision_polish import repair_vision_misreads
from researchlib.wechat_dialogue import present_stored_ocr

# 文章 24008 雪球截图里的那句。正文讲格力、美的、海尔，图里把美的认成了墓地。
_MIDEA_24008 = "这次大牛市搞了两个重仓亏损的股票，格力和墓地。"
# 文章 8880 群聊里同一句既写对了 free cash flow，又滑成 free case flow。
_CASH_8880 = (
    "把PE变成price/free cash flow就知道贵与不贵，本质还是噪音了free case flow 15倍，"
    "净利润转化为free cash flow的比率下降了。"
)


def test_grave_becomes_midea_beside_appliance_brands():
    assert repair_vision_misreads(_MIDEA_24008) == "这次大牛市搞了两个重仓亏损的股票，格力和美的。"
    assert polish_display_text(_MIDEA_24008) == "这次大牛市搞了两个重仓亏损的股票，格力和美的。"
    assert "美的" in normalize_vision_text(_MIDEA_24008)
    assert "墓地" not in normalize_vision_text(_MIDEA_24008)
    assert repair_vision_misreads("竞争对手除了墓地和海尔，还有小米") == "竞争对手除了美的和海尔，还有小米"
    assert repair_vision_misreads("格力、墓地、海尔都在降价") == "格力、美的、海尔都在降价"
    assert repair_vision_misreads("墓地空调卖得便宜") == "美的空调卖得便宜"
    assert repair_vision_misreads("墓地集团的方洪波") == "美的集团的方洪波"


def test_bare_grave_stays():
    alone = "他周末去墓地扫墓"
    assert repair_vision_misreads(alone) == alone
    assert polish_display_text(alone) == alone
    separated = "扫了墓地，格力也清了"
    assert repair_vision_misreads(separated) == separated
    district = "公墓地区有格力专卖店"
    assert repair_vision_misreads(district) == district
    assert "墓地" in district


def test_free_case_flow_becomes_free_cash_flow():
    fixed = repair_vision_misreads(_CASH_8880)
    assert "free case flow" not in fixed
    assert fixed.count("free cash flow") == 3
    assert polish_display_text(_CASH_8880) == fixed
    assert "free case flow" not in normalize_vision_text("方伟：" + _CASH_8880)
    assert repair_vision_misreads("FREE CASE FLOW") == "FREE CASH FLOW"
    assert repair_vision_misreads("Free Case Flow") == "Free Cash Flow"
    only = "本质还是噪音了free case flow 15倍"
    assert repair_vision_misreads(only) == "本质还是噪音了free cash flow 15倍"


def test_other_case_and_cash_spellings_stay():
    sentence = "in case the flow is free, free cash flow stays"
    assert repair_vision_misreads(sentence) == sentence
    assert repair_vision_misreads("free case") == "free case"
    assert repair_vision_misreads("free care flow") == "free care flow"
    assert repair_vision_misreads("free cash flow") == "free cash flow"


def test_typo_table_does_not_own_these_repairs():
    assert fix_obvious_typos("格力和墓地") == "格力和墓地"
    assert fix_obvious_typos("free case flow") == "free case flow"
    both = "AI算力是一个很新都东西，格力和墓地，free case flow"
    assert polish_display_text(both) == "AI算力是一个很新的东西，格力和美的，free cash flow"


def test_present_path_picks_up_stored_ocr():
    """阅读页旁路走 present_stored_ocr → polish_display_text。不改 image_texts。"""
    grave = present_stored_ocr("清仓格力\n" + _MIDEA_24008)
    assert "格力和美的" in grave
    assert "墓地" not in grave
    cash = present_stored_ocr("方伟：" + _CASH_8880)
    assert "free case flow" not in cash
    assert "free cash flow" in cash


def test_repair_is_idempotent():
    once = repair_vision_misreads(_MIDEA_24008 + _CASH_8880)
    assert repair_vision_misreads(once) == once

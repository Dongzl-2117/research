"""展示层 / 识图共用的明显错字修正。"""

from __future__ import annotations

from researchlib.present_text import polish_display_text
from researchlib.textutil import collapse_short_spam, fix_obvious_typos
from researchlib.wechat_dialogue import present_stored_ocr


def test_fix_henxin_de_dongxi():
    assert fix_obvious_typos("AI算力是一个很新都东西") == "AI算力是一个很新的东西"
    assert "很新的东西" in fix_obvious_typos("# AI算力是一个很新都东西\n\nAI算力是一个很新都东西，其实")
    assert polish_display_text("AI算力是一个很新都东西") == "AI算力是一个很新的东西"


def test_fix_yunying_ganggan():
    assert fix_obvious_typos("存在运用杠杆的好处") == "存在运营杠杆的好处"
    assert polish_display_text("存在运用杠杆的好处") == "存在运营杠杆的好处"


def test_fix_sixiang_lingnan():
    assert fix_obvious_typos("来源：思想?岭南") == "来源：思想×岭南"
    assert fix_obvious_typos("思想？岭南") == "思想×岭南"


def test_collapse_shenke_fanxing_soft_wrap():
    spam = "深刻反省，深刻反省，深刻\n反省，深刻反省，深刻反省"
    assert collapse_short_spam(spam).count("深刻反省") == 1


def test_present_stored_ocr_applies_typo_and_spam_collapse():
    raw = "领教工坊\nINSIDE THE BEST\n清仓格力\n深刻反省，深刻反省，深刻反省，深刻反省"
    out = present_stored_ocr(raw)
    assert "领教工坊" not in out
    assert "INSIDE" not in out.upper()
    assert out.count("深刻反省") <= 1
    assert "很新的东西" in present_stored_ocr("AI算力是一个很新都东西")

"""Literal arithmetic must survive the actual ingestion plain-text path."""

import pytest

from researchlib.ingest.store import _body_text
from researchlib.render import render_body
from researchlib.textutil import markdown_to_text


@pytest.mark.parametrize("formula", [
    "最终净资产 = 初始净资产 * (1+ROE)持有期",
    "100 * 1.06 * 40 = 4240",
    "100*1.06*40=4240",
    "本金*年数*收益率",
    "a*b*c",
    "α*β*γ",
    "2**3**4",
    "100∗(1+0.06)40 ≈ 1028.57",
    "100×1.06÷2−1 ≤ 53",
    "CAGR_1 = (1028.57 / 100)(1/40)−1",
    "未配对的星号 * 保留，~~ 未配对也保留",
])
def test_math_operators_and_quantities_survive(formula):
    assert markdown_to_text(formula) == formula
    assert _body_text("示例", formula) == formula


@pytest.mark.parametrize("source,expected", [
    ("**公式**：最终净资产 = 初始净资产 * (1+ROE)持有期", "公式：最终净资产 = 初始净资产 * (1+ROE)持有期"),
    ("*斜体* 与 **强调** 和 ***双重***。", "斜体 与 强调 和 双重。"),
    ("普通 *English emphasis* 与 **English strong**。", "普通 English emphasis 与 English strong。"),
    ("回报率**完全等于**公司的收益率。", "回报率完全等于公司的收益率。"),
    ("**outer *inner* outer**", "outer inner outer"),
    ("*first\nsecond* 与 **two\nlines**", "first\nsecond 与 two\nlines"),
    ("**outer *inner\nline* outer**", "outer inner\nline outer"),
    ("~~删除的内容~~ 与普通文字", "删除的内容 与普通文字"),
    ("- **公式**：100 * 2\n* *变量*：20 * 3\n+ 普通列表", "公式：100 * 2\n变量：20 * 3\n普通列表"),
    ("> **公式**：100 * 2", "公式：100 * 2"),
])
def test_real_emphasis_and_list_markers_are_removed(source, expected):
    assert markdown_to_text(source) == expected
    assert _body_text("示例", source) == expected


@pytest.mark.parametrize("rule", ["***", "* * *", "___", "_ _ _", "---", "- - -"])
def test_real_thematic_rules_are_removed(rule):
    assert markdown_to_text(f"上文\n\n{rule}\n\n下文") == "上文\n\n下文"
    assert markdown_to_text(f"> {rule}\n\n下文") == "下文"


@pytest.mark.parametrize("literal", [r"\*乘号\*", r"\*\*字面示例\*\*", r"100 \* 2"])
def test_escaped_stars_remain_literal(literal):
    assert markdown_to_text(literal) == literal


@pytest.mark.parametrize("code", ["100*2*3", "**literal**", "*literal*", "***", "~~literal~~"])
@pytest.mark.parametrize("ticks", ["`", "``"])
def test_inline_code_does_not_become_markup(code, ticks):
    assert markdown_to_text(f"{ticks}{code}{ticks} 与 **强调**") == f"{code} 与 强调"


def test_existing_fenced_code_policy_and_reader_math_are_unchanged():
    assert markdown_to_text("前文\n\n```text\n100*2\n```\n\n后文").split() == ["前文", "后文"]
    source = "**公式**：最终净资产 = 初始净资产 * (1+ROE)持有期"
    html = str(render_body(source, {}))
    assert "<strong>公式</strong>" in html
    assert "初始净资产 * (1+ROE)持有期" in html

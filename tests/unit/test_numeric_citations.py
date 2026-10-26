"""Numeric evidence must preserve quantities and units, without implying entailment."""

import pytest

from researchlib.citecheck import check_answer, quote_spans
from researchlib.web.quoting import mark_quoted


def numeric_flags(claim, source, **metadata):
    citation = {"n": 1, "text": source, **metadata}
    return [problem for flag in check_answer(claim + "[1]。", [citation], [])
            for problem in flag["problems"] if problem.startswith("数字 ")]


@pytest.mark.parametrize("source,claim", [
    ("本项费用是20元。", "本项费用是20%"),
    ("增长率为20%。", "增长率为20个百分点"),
    ("增长率为20个百分点。", "增长率为20%"),
    ("本项比例是20‰。", "本项比例是20%"),
    ("The multiple was20 in the table.", "The multiple was20倍"),
    ("The change was25bpm in this example.", "The change was25bp"),
    ("The value was1.2 in this example.", "The value was12"),
    ("甲组为12，乙组为34。", "本项数值是1234"),
    ("甲组为12。\n乙组为34。", "本项数值是1234"),
    ("The independent values were1 and20.", "The value was120"),
    ("The value was1 200.5 in this example.", "The value was1200.5"),
    ("本项变化为-20%。", "本项变化为20%"),
    ("本项变化为20%。", "本项变化为-20%"),
    ("The version is1.2.3.", "The value is12"),
    ("The version is1.2.3.", "The value is1.2"),
    ("The value is20.5%.", "The value is20%"),
    ("The value is120%.", "The value is20%"),
    ("The value is6.0.", "The value is5.0"),
    ("本项记录25bpm左右。", "本项记录25bp左右"),
    ("本项记录25bp_code。", "本项记录25bp左右"),
    ("本项记录20percentage pointsmith。", "本项记录20percentage points左右"),
])
def test_wrong_unit_sign_or_joined_number_needs_review(source, claim):
    assert numeric_flags(claim, source)


@pytest.mark.parametrize("source,claim", [
    ("本项增长为20%。", "本项增长为20%"),
    ("本项增长为２０％。", "本项增长为20%"),
    ("本项变化为−20%。", "本项变化为-20%"),
    ("本项变化为＋20%。", "本项变化为20%"),
    ("本项变化为20%。", "本项变化为+20%"),
    ("本项增长为20个百分点。", "本项增长为20个百分点"),
    ("The change was25bps.", "The change was25bp"),
    ("The change was25 basis points.", "The change was25bp"),
    ("The growth was20 percent.", "The growth was20%"),
    ("The growth was20 percentage points.", "The growth was20个百分点"),
    ("本项变化为25基点。", "本项变化为25bp"),
    ("The value was1,200.5 dollars.", "The value was1200.5 dollars"),
    ("The value was1\u202f200.5 dollars.", "The value was1200.5 dollars"),
    ("The value was1\u00a0200.5 dollars.", "The value was1200.5 dollars"),
    ("本项比例为.5%。", "本项比例为0.5%"),
    ("本项比例为－．５％。", "本项比例为-0.5%"),
    ("The value is1.2.3 in a version string.", "The value is1.2.3 in a version string"),
    ("The two values are20,30.", "The two values are20 and30"),
    ("两个数是20，30。", "两个数是20和30"),
    ("The value is20.5%.", "The value is20.5%"),
    ("The value is120%.", "The value is120%"),
    ("The value is20.0%.", "The value is20%"),
    ("The value is20%.", "The value is20.0%"),
    ("本项比例为-0.50%。", "本项比例为-0.5%"),
    ("The value is00020.500%.", "The value is20.5%"),
    ("The value is5.", "The value is5.0"),
    ("本项变化25bp左右。", "本项变化25bp"),
    ("本项变化25bps左右。", "本项变化25bp"),
    ("本项增长20percent左右。", "本项增长20%"),
    ("本项变化20percentage points左右。", "本项变化20个百分点"),
])
def test_faithful_numeric_formatting_and_units_remain_usable(source, claim):
    assert not numeric_flags(claim, source)


def test_title_or_heading_cannot_supply_a_missing_numeric_anchor():
    assert numeric_flags("本项收入增长20%", "本项只讨论收入，没有披露增长数值。",
                         title="收入增长20%", heading="20%")


def test_one_citation_context_can_supply_an_actual_numeric_anchor():
    assert not numeric_flags("本项收入增长20%", "本项讨论经营收入。", before="表中收入增长20%。")


@pytest.mark.parametrize("source,quote", [
    ("The recorded rate was.5% in this example.", "The recorded rate was5% in this example."),
    ("本项变化为．５％，仍需观察。", "本项变化为5%，仍需观察。"),
])
def test_leading_decimal_point_is_not_ignored_for_a_quote(source, quote):
    assert quote_spans(source, quote) is None
    assert mark_quoted("<p>" + source + "</p>", quote)[1] is False


@pytest.mark.parametrize("source,quote", [
    ("The recorded rate was.5% in this example.", "The recorded rate was0.5% in this example."),
    ("本项变化为－．５％，仍需观察。", "本项变化为-0.5%，仍需观察。"),
])
def test_leading_zero_is_a_typographic_decimal_difference(source, quote):
    assert quote_spans(source, quote)
    assert mark_quoted("<p>" + source + "</p>", quote)[1]

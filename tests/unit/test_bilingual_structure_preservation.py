"""复查反例：同文的标题、正文及不同换行结构不能当作同一对删除。"""

import pytest

from researchlib.translate import qa
from researchlib.translate.display import PairDisplay, article_pairs, protected_content_gaps
from researchlib.translate.pair import display_blocks, pairs_from_blocks, redundant_pairs

CASES = [
    ("<h2>Important summary</h2><p>Important summary</p>", "<h2>重要总结</h2><p>重要总结</p>"),
    ("<p>Important summary</p><h2>Important summary</h2>", "<p>重要总结</p><h2>重要总结</h2>"),
    ("<p>Line one<br>Line two</p><p>Line oneLine two</p>", "<p>第一行<br>第二行</p><p>第一行第二行</p>"),
    ("<p>Line one<br>Line two</p><p>Line oneLine<br> two</p>",
     "<p>第一行<br>第二行</p><p>第一行第二<br>行</p>"),
    ("<h2>Important summary</h2><h3>Important summary</h3>", "<h2>重要总结</h2><h3>重要总结</h3>"),
]


@pytest.mark.parametrize(("original", "translated"), CASES)
def test_structure_distinct_pairs_are_both_preserved(original, translated):
    view = article_pairs(original, translated)
    assert not view.notice
    assert len(view.pairs) == 2
    assert redundant_pairs(view.pairs) == []
    assert "".join(pair.en for pair in view.pairs) == original
    assert "".join(pair.zh or "" for pair in view.pairs) == translated


@pytest.mark.parametrize(("original", "translated"), CASES)
def test_qa_measures_both_structure_distinct_occurrences(original, translated):
    report = qa.report_from_html(1, "synthetic", "合成结构", original, translated, mapping_status="recorded")
    assert report.pairs == 2
    assert report.source_projection_loss == report.translation_projection_loss == 0
    assert report.status == "pass"


@pytest.mark.parametrize(("original", "translated"), CASES)
def test_qa_rejects_a_dropped_structurally_distinct_occurrence(monkeypatch, original, translated):
    blocks = display_blocks(original, translated, dedupe=False)
    raw = pairs_from_blocks(*blocks)
    monkeypatch.setattr(qa, "article_pairs", lambda *args, **kwargs: PairDisplay(raw[:1], mapping_status="recorded"))
    report = qa.report_from_html(1, "synthetic", "合成结构", original, translated, mapping_status="recorded")
    assert report.status == "fail"
    assert report.source_projection_loss > 0
    assert report.translation_projection_loss > 0


def test_equivalent_markup_and_read_time_anchors_can_still_fold():
    text = "Important summary of the synthetic business report covers distribution, revenue and operating costs."
    original = f'<p class="summary" id="p-1">{text}</p><p id="p-2" class="summary">{text}</p>'
    translated = '<p class="summary" id="p-1">合成总结。</p><p id="p-2" class="summary">合成总结。</p>'
    view = article_pairs(original, translated)
    assert not view.notice
    assert len(view.pairs) == 1


@pytest.mark.parametrize("header", ["", "<tr><th>Period</th><th>Revenue</th></tr>"])
def test_intact_numeric_table_is_counted_once_and_does_not_request_retranslation(header):
    table = f"<table>{header}<tr><td>10</td><td>20</td></tr></table>"
    original = table + "<p>Sales overview.</p>"
    translated = table.replace("Period", "期间").replace("Revenue", "收入") + "<p>销售概览。</p>"
    assert protected_content_gaps(original, translated) == 0
    report = qa.report_from_html(1, "synthetic", "合成表格", original, translated, mapping_status="recorded")
    assert report.status == "pass"
    assert qa.classify_legacy(report)["category"] == "recorded"
    legacy = qa.report_from_html(1, "synthetic", "合成表格", original, translated)
    assert legacy.status == "warn"
    assert qa.classify_legacy(legacy)["category"] == "lossless-display-only"


@pytest.mark.parametrize("header", ["", "<tr><th>Period</th><th>Revenue</th></tr>"])
def test_missing_numeric_cell_is_still_rejected(header):
    table = f"<table>{header}<tr><td>10</td><td>20</td></tr></table>"
    original = table + "<p>Sales overview.</p>"
    translated = table.replace("<td>20</td>", "").replace("Period", "期间").replace("Revenue", "收入")
    translated += "<p>销售概览。</p>"
    assert protected_content_gaps(original, translated) > 0
    report = qa.report_from_html(1, "synthetic", "合成表格", original, translated, mapping_status="recorded")
    assert report.status == "fail"
    assert "protected-content-loss" in {issue.code for issue in report.issues}
    assert qa.classify_legacy(report)["category"] == "retranslation-review"

"""合并 #221／#222 时保留本地化数字、拒绝错位，并维持安全展示去重。"""

import pytest

from researchlib.translate import provenance as pr
from researchlib.translate.display import article_pairs
from researchlib.translate.pair import bilingual_pairs
from researchlib.translate.qa import report_from_html

FILLER_EN = '<p>Distribution costs and the installed base shape regional customer retention over the whole year.</p>'
FILLER_ZH = '<p>分销成本和已有用户影响各地区整年的客户留存。</p>'

LOCALIZED = [
    ('<p>Revenue was $300,000.</p>', '<p>收入为30万美元。</p>'),
    ('<p>Revenue was $69.1 billion.</p>', '<p>收入为691亿美元。</p>'),
    ('<p>Revenue was $10 million.</p>', '<p>收入为1000万美元。</p>'),
    ('<p>Success was 100%.</p>', '<p>成功率是百分之百。</p>'),
    ('<p>September 9, 2026 was the report date.</p>', '<p>报告日期是2026年9月9日。</p>'),
    ('<p>FY2026 Q3 revenue was 10 dollars.</p>', '<p>2026财年第三季度收入为10美元。</p>'),
]


@pytest.mark.parametrize(('source', 'target'), LOCALIZED)
def test_equivalent_localized_values_preserve_positional_pairs_in_a_short_article(source, target):
    assert not pr.positional_numbers_conflict([source], [target])
    assert bilingual_pairs(source, target) is not None
    view = article_pairs(source, target)
    assert view.notice == '' and view.correspondence == 'legacy'
    assert len(view.pairs) == 1 and view.pairs[0].zh


def test_localized_values_and_repeated_filler_keep_the_merged_dedup_behavior():
    source = ''.join(item[0] for item in LOCALIZED) + FILLER_EN * 8
    target = ''.join(item[1] for item in LOCALIZED) + FILLER_ZH * 8
    view = article_pairs(source, target)
    assert view.notice == '' and view.correspondence == 'legacy'
    assert len(view.pairs) == len(LOCALIZED) + 1
    assert sum('Distribution costs' in pair.en for pair in view.pairs) == 1
    report = report_from_html(1, 'synthetic', '合成报告', source, target)
    assert report.status == 'warn' and not report.display_notice


@pytest.mark.parametrize(('source', 'target'), [
    ('<p>Profit was 10 dollars.</p>', '<p>利润为20美元。</p>'),
    ('<p>Revenue was $69.1 billion.</p>', '<p>收入为690亿美元。</p>'),
    ('<p>Success was 100%.</p>', '<p>成功率是百分之九十。</p>'),
    ('<p>September 9, 2026 was the report date.</p>', '<p>报告日期是2026年9月10日。</p>'),
    ('<p>FY2026 Q3 revenue was 10 dollars.</p>', '<p>2026财年第四季度收入为10美元。</p>'),
    ('<p>Profit was 10 dollars.</p>', '<p>利润为10美元，另有10美元。</p>'),
])
def test_a_single_wrong_leaf_is_not_excused_by_many_correct_paragraphs(source, target):
    sources = [source] + [FILLER_EN] * 8
    targets = [target] + [FILLER_ZH] * 8
    assert pr.positional_numbers_conflict(sources, targets)
    assert bilingual_pairs(''.join(sources), ''.join(targets)) is None
    view = article_pairs(''.join(sources), ''.join(targets))
    assert view.notice and view.correspondence == 'unmapped'
    assert all(not (pair.en and pair.zh) for pair in view.pairs)
    assert source in view.pairs[0].en and target in (view.pairs[1].zh or '')


def test_simultaneous_link_and_numeric_errors_still_fail_before_display_dedup():
    source = '<p>Profit was 10 dollars. <a href="https://example.invalid/one">Report</a></p>'
    target = '<p>利润为20美元。<a href="https://example.invalid/two">报告</a></p>'
    view = article_pairs(source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert view.notice and len(view.pairs) == 2
    assert 'example.invalid/one' in view.pairs[0].en
    assert 'example.invalid/two' in (view.pairs[1].zh or '')
    report = report_from_html(1, 'synthetic', '合成报告', source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert report.status == 'fail' and report.display_notice


def test_equivalent_values_moved_to_another_leaf_still_fail_position_checks():
    sources = ['<p>Revenue was $300,000.</p>', '<p>Cash was $200,000.</p>'] + [FILLER_EN] * 8
    targets = ['<p>收入为20万美元。</p>', '<p>现金为30万美元。</p>'] + [FILLER_ZH] * 8
    assert pr.positional_numbers_conflict(sources, targets)


def test_display_localization_does_not_relax_formal_receipt_fact_requirements():
    source, target = LOCALIZED[0]
    assert pr.leaf_problem(source, target) == 'numbers'
    with pytest.raises(pr.AlignmentError, match='numbers'):
        pr.receipt(pr.source_manifest(source), target)


def test_no_anchors_do_not_claim_semantics_and_leaf_count_mismatch_is_rejected():
    assert not pr.positional_numbers_conflict([FILLER_EN], [FILLER_ZH])
    assert pr.positional_numbers_conflict([FILLER_EN], [])


def test_very_long_amounts_do_not_round_to_the_same_value():
    source = '<p>Revenue was 1234567890123456789012345678901 dollars.</p>'
    target = '<p>收入为1234567890123456789012345678902美元。</p>'
    assert pr.positional_numbers_conflict([source], [target])
    source = '<p>Revenue was 123456789012345678901234567.8901万 dollars.</p>'
    target = '<p>收入为1234567890123456789012345678901美元。</p>'
    assert not pr.positional_numbers_conflict([source], [target])


def test_invalid_dates_do_not_crash_or_match_a_different_valid_date():
    source = '<p>September 31, 2026 was the report date.</p>'
    target = '<p>报告日期是2026年9月30日。</p>'
    assert pr.positional_numbers_conflict([source], [target])
    assert not pr.positional_numbers_conflict([source], [source])

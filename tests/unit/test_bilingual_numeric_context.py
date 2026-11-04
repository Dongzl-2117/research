"""数字本地化须看语境：缩写年份与版本标识符不能当金额处理。"""

import pytest

from researchlib.translate import provenance as pr
from researchlib.translate.display import article_pairs
from researchlib.translate.pair import bilingual_pairs
from researchlib.translate.qa import report_from_html

FILLER_EN = '<p>Distribution costs and the installed base shape regional customer retention over the whole year.</p>'
FILLER_ZH = '<p>分销成本和已有用户影响各地区整年的客户留存。</p>'


@pytest.mark.parametrize(('source', 'target'), [
    ("The company was founded in '48.", '公司于1948年成立。'),
    ('The company was founded in ’48.', '公司于1948年成立。'),
    ('The company was founded in ‘48.', '公司于1948年成立。'),
    ("The company has operated since '08.", '公司自2008年开始经营。'),
    ('The company was founded in 1948.', '公司于1948年成立。'),
    ('Version 3.10 is available.', '版本3.10已发布。'),
    ('Version 3.10.2 is available.', '版本3.10.2已发布。'),
    ('v3.10 is available.', '版本3.10已发布。'),
    ('Version 3.10 is available.', '3.10版本已发布。'),
    ('Version: 1.0-rc1 is available.', '版本1.0-rc1已发布。'),
    ('The price was 3.10 dollars.', '价格为3.1美元。'),
    ('The success rate was 100.0%.', '成功率是百分之百。'),
])
def test_contextual_equivalence_keeps_pairs_and_main_dedup(source, target):
    source, target = f'<p>{source}</p>', f'<p>{target}</p>'
    assert not pr.positional_numbers_conflict([source], [target])
    assert bilingual_pairs(source, target) is not None
    view = article_pairs(source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert view.notice == '' and view.correspondence == 'legacy'
    assert len(view.pairs) == 2
    assert source in view.pairs[0].en and view.pairs[0].zh
    report = report_from_html(1, 'synthetic', '合成报告', source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert report.status == 'warn' and report.semantic_status == 'not-reviewed'


@pytest.mark.parametrize(('source', 'target'), [
    ("The company was founded in '48.", '公司于1949年成立。'),
    ("The company was founded in '48.", '公司有1948美元。'),
    ('The company paid 48 dollars.', '公司支付1948美元。'),
    ("The count was quoted in '48' units.", '数量是1948年。'),
    ('The company was founded in 1948.', '公司于2048年成立。'),
    ("The company was founded in '48.", '公司于1948年成立，1949年开始经营。'),
    ('Version 3.10 is available.', '版本3.1已发布。'),
    ('Version 3.10.2 is available.', '版本3.1.2已发布。'),
    ('v3.10 is available.', '版本3.1已发布。'),
    ('Version 3.10 is available.', '版本3.10.0已发布。'),
    ('Version 03.10 is available.', '3.10版本已发布。'),
    ('Version 1.0-rc1 is available.', '版本1.0-rc2已发布。'),
    ('Version 1.0 is available.', '版本1已发布。'),
    ('Version 3.10 is available.', '价格是3.10美元。'),
    ('The price was 3.10 dollars.', '价格为3.2美元。'),
])
def test_context_does_not_hide_changes_or_invent_years(source, target):
    source, target = f'<p>{source}</p>', f'<p>{target}</p>'
    sources, targets = [source] + [FILLER_EN] * 8, [target] + [FILLER_ZH] * 8
    assert pr.positional_numbers_conflict(sources, targets)
    assert bilingual_pairs(''.join(sources), ''.join(targets)) is None
    view = article_pairs(''.join(sources), ''.join(targets))
    assert view.notice and view.correspondence == 'unmapped'
    assert all(not (pair.en and pair.zh) for pair in view.pairs)
    assert source in view.pairs[0].en and target in (view.pairs[1].zh or '')


def test_wrong_version_and_link_cannot_be_hidden_by_other_correct_pairs():
    source = '<p>Version 3.10 <a href="https://example.invalid/one">release notes</a>.</p>'
    target = '<p>版本3.1<a href="https://example.invalid/two">发行说明</a>。</p>'
    report = report_from_html(1, 'synthetic', '合成报告', source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert report.status == 'fail' and report.display_notice


def test_year_expansion_remains_read_only_and_cannot_issue_a_new_receipt():
    source, target = "<p>The company was founded in '48.</p>", '<p>公司于1948年成立。</p>'
    with pytest.raises(pr.AlignmentError, match='numbers'):
        pr.receipt(pr.source_manifest(source), target)

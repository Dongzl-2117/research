"""不把未标记的数字标识符当金额；版本等值不依赖固定中文连接词。"""

import pytest

from researchlib.translate import provenance as pr
from researchlib.translate.display import article_pairs
from researchlib.translate.qa import report_from_html

FILLER_EN = '<p>Distribution costs and the installed base shape regional customer retention over the whole year.</p>'
FILLER_ZH = '<p>分销成本和已有用户影响各地区整年的客户留存。</p>'


@pytest.mark.parametrize(('source', 'target'), [
    ('Version 3.10 is affected.', '受影响的版本是3.10。'),
    ('Version 3.10 is affected.', '版本：3.10。'),
    ('Version 3.10 is affected.', '版本为3.10。'),
    ('Version 3.10 is affected.', '版本号对应的是3.10。'),
    ('Version 3.10.2 is affected.', '受影响的版本是3.10.2。'),
    ('Version 1 is affected.', '受影响的版本是1。'),
    ('Python3.10 is affected.', 'Python3.10受到影响。'),
    ('Python 3.10 is affected.', 'Python 3.10受到影响。'),
    ('Python3.10 is affected.', '受影响的版本为3.10。'),
    ('The price was $3.10.', '价格为3.1美元。'),
    ('The price was USD3.10.', '价格为3.1美元。'),
    ('The price was 3.10 dollars.', '价格为3.1美元。'),
    ('The price was 3.10 million dollars.', '价格为310万美元。'),
    ('The price was 10 dollars.', '价格为10。'),
])
def test_literals_keep_equivalent_identifiers_and_explicit_quantities(source, target):
    source, target = f'<p>{source}</p>', f'<p>{target}</p>'
    assert not pr.positional_numbers_conflict([source], [target])
    view = article_pairs(source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert not view.notice and view.correspondence == 'legacy' and len(view.pairs) == 2
    report = report_from_html(1, 'synthetic', '合成报告', source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert report.status == 'warn' and report.semantic_status == 'not-reviewed'


@pytest.mark.parametrize(('source', 'target'), [
    ('Version 3.10 is affected.', '受影响的版本是3.1。'),
    ('Version 3.10 is affected.', '版本：3.1。'),
    ('Version 3.10 is affected.', '版本为3.1。'),
    ('Version 3.10.2 is affected.', '受影响的版本是3.1.2。'),
    ('Version 1.0 is affected.', '受影响的版本是1。'),
    ('Python3.10 is affected.', 'Python3.1受到影响。'),
    ('Python 3.10 is affected.', 'Python 3.1受到影响。'),
    ('Python3.10.2 is affected.', 'Python3.1.2受到影响。'),
    ('H0100 is affected.', 'H100受到影响。'),
    ('The unmarked value was 3.10.', '没有单位的数值为3.1。'),
    ('The price was $3.10.', '价格为3.2美元。'),
    ('The price was 3.10 million dollars.', '价格为301万美元。'),
])
def test_normal_pairs_cannot_hide_an_identifier_or_quantity_change(source, target):
    source, target = f'<p>{source}</p>', f'<p>{target}</p>'
    assert pr.positional_numbers_conflict([source] + [FILLER_EN] * 8, [target] + [FILLER_ZH] * 8)
    view = article_pairs(source + FILLER_EN * 8, target + FILLER_ZH * 8)
    assert view.notice and view.correspondence == 'unmapped'
    assert source in view.pairs[0].en and target in (view.pairs[1].zh or '')

"""全站复核的最小保真反例；只使用合成 HTML。"""

import pytest

from researchlib.translate import provenance as pr
from researchlib.translate import service
from researchlib.translate.display import protected_content_gaps
from researchlib.translate.qa import report_from_html
from researchlib.translate.segments import iter_segments, shield, splice_segments, translatable_blocks, unshield


@pytest.mark.parametrize('value', [
    '<p>Literal id="p-7" stays in the sentence.</p>',
    '<p>Use <code>id="p-7"</code> in this example.</p>',
    '<p><a id="p-7" href="#p-7">Original named target.</a></p>',
])
def test_canonical_source_keeps_real_anchor_and_literal_text(value):
    assert pr.canonical_html(value) == value


def test_editing_inline_markers_survive_materialization_and_translation():
    source = '<p>Use <del>old plan</del> and <ins>new plan</ins>.</p>'
    target = '<p>使用<del>旧计划</del>和<ins>新计划</ins>。</p>'
    assert pr.materialize(source) == source
    assert splice_segments(source, {0: target}) == target


def test_direct_div_text_is_a_leaf_without_flattening_nested_blocks():
    source = '<div>Direct English sentence.</div><div><p>Nested sentence.</p></div>'
    assert len(iter_segments(source)) == len(translatable_blocks(source)) == 2
    target = splice_segments(source, {0: '<div>直接中文句子。</div>', 1: '<p>内层句子。</p>'})
    assert target == '<div>直接中文句子。</div><div><p>内层句子。</p></div>'


def test_mixed_div_text_is_not_lost_beside_block_children():
    source = '<div id="original-note">First <em>note</em>.<p>Middle paragraph.</p>Last note.</div>'
    manifest = pr.source_manifest(source)
    assert len(manifest['segments']) == 3
    assert '<em>note</em>' in manifest['source_snapshot'] and 'id="original-note"' in manifest['source_snapshot']
    target = splice_segments(manifest['source_snapshot'], {
        0: '<p>第一条<em>注释</em>。</p>', 1: '<p>中间段落。</p>', 2: '<p>最后注释。</p>',
    })
    assert pr.verify(source, target, pr.receipt(manifest, target)).status == 'mapped'
    assert 'First' not in target and 'Last' not in target and 'id="original-note"' in target


def test_code_whitespace_survives_all_receipt_stages():
    source = '<p>Use <code>a  b\n c</code> here.</p>'
    target = '<p>这里使用<code>a  b\n c</code>。</p>'
    assert pr.materialize(source) == source
    assert splice_segments(source, {0: target}) == target
    assert pr.verify(source, target, pr.receipt(pr.source_manifest(source), target)).status == 'mapped'


def test_non_anchor_named_footnotes_are_restored_or_rejected():
    source = '<p><sup id="back-1">1</sup> Original prose <span id="note-1">note</span>.</p>'
    protected = shield(source)
    # 模型保留协议标签和身份，翻译其文字；原始目标 ID 在恢复后必须仍存在。
    target = unshield(protected.html.replace('Original prose', '原文').replace('note</span>', '注释</span>'), protected)
    assert target and 'id="back-1"' in target and 'id="note-1"' in target
    assert pr.leaf_problem(source, target) is None
    assert pr.leaf_problem(source, target.replace('id="note-1"', '')) == 'anchors'


@pytest.mark.parametrize('target', ['<p>123</p>', '<p>...</p>', '<p></p>'])
def test_no_text_output_is_rejected_before_it_can_retain_english_as_complete(target):
    assert service.translation_problem(['<p>Original English.</p>'], [target])


def test_protected_giant_code_is_not_a_giant_prose_pair():
    code = '<pre><code>' + 'synthetic_function()  \n' * 400 + '</code></pre>'
    report = report_from_html(1, 'synthetic', '合成', code + '<p>Example.</p>', code + '<p>示例。</p>')
    assert report.giant == ()
    assert 'protected-content-loss' not in {issue.code for issue in report.issues}
    missing = report_from_html(1, 'synthetic', '合成', code, '')
    assert 'protected-content-loss' in {issue.code for issue in missing.issues}


@pytest.mark.parametrize('cells', ['<td>20</td><td>10</td>', '<td>10</td>', '<td>10</td><td>10</td>',
                                   '<td>10</td><td>20</td><td>20</td>', '<td>10</td><td>20%</td>'])
def test_legacy_numeric_tables_keep_order_multiplicity_and_punctuation(cells):
    source = '<table><tr><td>10</td><td>20</td></tr></table>'
    assert protected_content_gaps(source, '<table><tr>' + cells + '</tr></table>') > 0


@pytest.mark.parametrize('table', [
    '<table><tr><td>10</td><td>20</td></tr></table>',
    '<table><tr><th>Sales</th><td>10%</td><td>10%</td></tr></table>',
])
def test_intact_numeric_tables_do_not_raise_a_false_qa_failure(table):
    assert protected_content_gaps(table, table.replace('Sales', '销售额')) == 0

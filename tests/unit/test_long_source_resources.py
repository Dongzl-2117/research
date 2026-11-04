"""实际长段拆句必须保留代码字节和原始命名锚点。"""

import pytest

from researchlib.translate import provenance as pr
from researchlib.translate.display import article_pairs

PREFIX = 'A sentence about a long story. ' * 45


@pytest.mark.parametrize('tail', [
    'Use <code>a  b\n c</code> here. Another sentence.',
    '<span id="fn-1">Two sentences. Other words.</span>',
    'Some words.<span id="fn-1"></span>',
    'Some words.<sup id="fn-1"></sup>',
    '<a href="https://example.invalid/one">Two sentences. Other words.</a>',
    'Some words.<img src="/media/synthetic.png">',
])
def test_long_sentence_materialization_preserves_all_resources(tail):
    source = '<p>' + PREFIX + tail + '</p>'
    manifest = pr.source_manifest(source)
    assert len(manifest['segments']) >= 40
    before, after = pr.Resources(source), pr.Resources(manifest['source_snapshot'])
    assert (before.codes, before.links, before.anchors, before.media) == (
        after.codes, after.links, after.anchors, after.media,
    )
    proof = pr.receipt(manifest, manifest['source_snapshot'])
    assert pr.verify(source, manifest['source_snapshot'], proof).status == 'mapped'


def test_materialization_cannot_sign_a_snapshot_after_losing_original_anchors(monkeypatch):
    source = '<p>Source <span id="fn-1"></span> prose.</p>'
    real_splice = pr.splice_segments
    monkeypatch.setattr(pr, 'splice_segments', lambda value, slots: real_splice(value, slots).replace('id="fn-1"', ''))
    with pytest.raises(pr.AlignmentError, match='materialize-content'):
        pr.source_manifest(source)


@pytest.mark.parametrize('source,target', [
    ('<p>Literal id="p-7" stays in the sentence.</p>', '<p>句子中保留 id="p-7" 字样。</p>'),
    ('<p>Use <code>id="p-7"  x</code> here.</p>', '<p>这里使用<code>id="p-7"  x</code>。</p>'),
])
def test_mapped_and_fallback_display_keep_literal_paragraph_id_text(source, target):
    proof = pr.receipt(pr.source_manifest(source), target)
    for displayed in (article_pairs(source, target, provenance=proof),
                      article_pairs(source, target + '<p>另一个段落。</p>')):
        shown = ''.join(pair.en + (pair.zh or '') for pair in displayed.pairs)
        assert shown.count('id="p-7"') == 2
        assert pr.Resources(source).codes == pr.Resources(''.join(pair.en for pair in displayed.pairs)).codes


def test_fallback_keeps_real_named_paragraph_anchors():
    source = '<p id="p-7">Literal id="p-9" remains. <a href="#p-7">Return</a>.</p>'
    displayed = article_pairs(source, '<p>中文首段。</p><p>中文第二段。</p>')
    assert displayed.notice
    shown = ''.join(pair.en + (pair.zh or '') for pair in displayed.pairs)
    assert 'id="p-7"' in shown and 'href="#p-7"' in shown and 'id="p-9"' in shown

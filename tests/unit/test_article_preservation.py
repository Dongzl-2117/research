"""独立于翻译叶子计数的合成回归：原文内容、数字、结构和引用不能因对照丢失。"""

from html.parser import HTMLParser

from researchlib.render import render_body
from researchlib.translate.display import article_pairs, content_gaps
from researchlib.translate.pair import stamp_paragraph_ids
from researchlib.translate.qa import report_from_html
from researchlib.translate.segments import splice_segments


def _project(pairs, language):
    return "".join(getattr(pair, language) or "" for pair in pairs)


def test_bilingual_retains_numbers_images_code_headings_and_ordered_lists():
    source = (
        '<h1>Revenue report</h1><p>Before the table.</p>'
        '<table><tr><th>Year</th><th>Revenue</th></tr><tr><td>2026</td><td>123.45%</td></tr></table>'
        '<p><img src="/media/synthetic.png" alt="Revenue chart"></p>'
        '<pre><code>return 42 &lt; 100</code></pre>'
        '<ol start="4"><li>First item</li><li>99</li></ol><h6>Footnote</h6>'
        '<p>After the table.</p>'
    )
    translated = splice_segments(source, {0: "<p>表格前面。</p>", 1: "<th>年份</th>", 2: "<th>收入</th>",
                                            3: "<li>第一项</li>", 4: "<p>表格后面。</p>"})
    view = article_pairs(stamp_paragraph_ids(source), stamp_paragraph_ids(translated))
    en = _project(view.pairs, "en")
    assert content_gaps(source, en) == 0
    for content in ('<table>', '<td>2026</td>', '<td>123.45%</td>', '<ol start="4">', '<li>99</li>',
                    '<pre><code>', 'src="/media/synthetic.png"', '<h1>', '<h6>'):
        assert content in en
    assert en.index("Before") < en.index("123.45%") < en.index("synthetic.png") < en.index("return 42")
    assert en.index("return 42") < en.index("First item") < en.index("After")
    assert _project(view.pairs, "zh").count('src="/media/synthetic.png"') == 0


def test_missing_middle_translation_is_never_positionally_paired():
    source = stamp_paragraph_ids("<p>Alpha.</p><p>Beta.</p><p>Gamma.</p>")
    translated = stamp_paragraph_ids("<p>甲。</p><p>丙。</p>")
    view = article_pairs(source, translated)
    assert view.notice and len(view.pairs) == 2
    assert all(not (pair.en and pair.zh) for pair in view.pairs)
    assert content_gaps(source, _project(view.pairs, "en")) == 0
    assert content_gaps(translated, _project(view.pairs, "zh")) == 0
    assert 'id="p-2"' in _project(view.pairs, "zh")
    assert 'id="p-2"' not in _project(view.pairs, "en")


def test_partial_overlap_dedup_must_not_remove_unique_source_information():
    common = "A long explanation of market dynamics and distribution in a competitive industry. " * 2
    source = f"<p>{common}AlphaExclusive.</p><p>{common}BetaExclusive and more context.</p>"
    translated = "<p>这是一段共同说明，其中甲独有。</p><p>这是一段共同说明，其中乙独有以及更多背景。</p>"
    view = article_pairs(source, translated)
    assert content_gaps(source, _project(view.pairs, "en")) == 0


def test_identical_text_does_not_hide_a_different_translated_source_link():
    source = '<p>Read <a href="https://example.org/original">this report</a>.</p>'
    translated = '<p>Read <a href="https://example.org/translated">this report</a>.</p>'
    view = article_pairs(source, translated)
    assert 'href="https://example.org/translated"' in _project(view.pairs, "zh")


def test_structural_qa_catches_loss_even_when_translatable_leaf_counts_match():
    source = '<p>Revenue.</p><table><tr><td>2026</td><td>123.45</td></tr></table><pre>return 42</pre>'
    report = report_from_html(1, "synthetic", "Report", source, "<p>收入。</p>")
    assert report.en_leaves == report.zh_leaves == 1
    assert any(issue.code == "protected-content-loss" and issue.level == "fail" for issue in report.issues)


class _Outline(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def test_giant_imported_heading_becomes_complete_body_paragraph():
    body = "这是完整正文，不能放大成一整屏的标题。" * 30
    for fmt, raw in ((None, f"# {body}\n\n## 真正章节\n\n正文。"),
                     ("html", f'<h1 style="font-size:80px" id="source-heading">{body}</h1><h1>真正章节</h1>')):
        rendered = str(render_body(raw, {}, fmt))
        outline = _Outline(rendered)
        assert body in "".join(outline.text)
        assert "h1" not in {tag for tag, _attrs in outline.tags}
        assert "h2" in {tag for tag, _attrs in outline.tags}
        assert "80px" not in rendered
        assert rendered.startswith("<p")


def test_unavailable_table_placeholder_remains_explicit():
    rendered = str(render_body("[内嵌表格未取到]\n\n下一段。", {}))
    assert 'class="source-missing" role="note"' in rendered
    assert "内嵌表格未取到" in rendered and "原导出未包含此表格" in rendered
    assert "下一段。" in rendered and "<table" not in rendered

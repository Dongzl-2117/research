"""独立于翻译叶子计数的合成回归：原文内容、数字、结构和引用不能因对照丢失。"""

import re
from html.parser import HTMLParser

import pytest

from researchlib.render import render_body
from researchlib.translate import display
from researchlib.translate.display import article_pairs, content_gaps
from researchlib.translate.pair import BilingualPair, stamp_paragraph_ids
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


@pytest.mark.parametrize("kind", ["link", "image"])
def test_repeated_paragraphs_retain_distinct_translated_resources(kind):
    paragraph = "The same long synthetic paragraph explains how distribution and operating costs affect this market."
    source = f"<p>{paragraph}</p>" * 2
    resources = [f'<a href="/{name}">报告</a>' if kind == "link" else f'<img src="/media/{name}.png">'
                 for name in ("a", "b")]
    translated = "".join(f"<p>这两段译文可见文字完全相同。{resource}</p>" for resource in resources)
    view = article_pairs(source, translated)
    actual = "".join(pair.en + (pair.zh or "") for pair in view.pairs)
    assert len(view.pairs) == 2
    assert content_gaps(source, _project(view.pairs, "en")) == 0
    assert content_gaps(translated, actual) == 0
    for name in ("a", "b"):
        assert (f'href="/{name}"' if kind == "link" else f'src="/media/{name}.png"') in actual


def test_final_projection_guard_rejects_loss_from_a_later_block_transform(monkeypatch):
    source = "<p>Read this report.</p><pre>return 42</pre>"
    translated = '<p>阅读<a href="/translated-report">这份报告</a>。</p><pre>return 42</pre>'
    real_whole_pair = display._whole_pair

    def lose_translated_link(en, zh):
        pair = real_whole_pair(en, zh)
        return BilingualPair(pair.en, (pair.zh or "").replace('href="/translated-report"', ""), pair.anchor)

    # 模拟后续完整区块整理错误地删了 href；最终检查必须拒绝这个投影。
    monkeypatch.setattr(display, "_whole_pair", lose_translated_link)
    view = article_pairs(source, translated)
    actual = "".join(pair.en + (pair.zh or "") for pair in view.pairs)
    assert view.notice
    assert content_gaps(source, _project(view.pairs, "en")) == 0
    assert content_gaps(translated, actual) == 0
    assert all(not (pair.en and pair.zh) for pair in view.pairs)


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


_PARA_EN = (
    "<p>Paragraph {index} explains how distribution and operating costs shape the market "
    "in this synthetic article.</p>"
)
_PARA_ZH = "<p>第 {index} 段说明分销和运营成本如何影响这个合成例子里的市场。</p>"


def _paragraphs(count: int, *, offset: int = 1) -> tuple[str, str]:
    indexes = range(offset, offset + count)
    return (
        "".join(_PARA_EN.format(index=index) for index in indexes),
        "".join(_PARA_ZH.format(index=index) for index in indexes),
    )


def test_repeated_paragraph_is_collapsible_without_losing_its_occurrence():
    """只折叠完全相同的整对，出现位置及内容仍保留，其余逐段并排。"""
    en, zh = _paragraphs(40)
    source = en + _PARA_EN.format(index=7)
    translated = zh + _PARA_ZH.format(index=7)
    view = article_pairs(source, translated)
    assert view.notice == ""
    assert len(view.pairs) == 41
    assert view.pairs[-1].repeat_of == 6
    assert content_gaps(source, _project(view.pairs, "en")) == 0
    assert content_gaps(translated, "".join(pair.en + (pair.zh or "") for pair in view.pairs)) == 0


def test_near_duplicate_that_carries_unique_words_is_kept_without_falling_back():
    """21118 那类正文：英文一个块里用 <br> 分很多行，译文是很多段，整块对不上。

    近前缀的那一对带着独有的说法，收掉就会丢字。这时展示层要留着它，也不能整页退回。
    """
    common = (
        "The company reported strong device revenue growth across every region and the services business "
        "expanded at a double digit rate for the quarter"
    )
    short = f"{common} but falling margins."
    long = f"{common} and rising margins with steady demand."
    lines = [short, long] + [
        f"Line {index} of the synthetic article explains distribution costs in plain words." for index in range(1, 24)
    ]
    source = "<p>" + "<br>".join(lines) + "</p>"
    translated = (
        "<p>公司各地区的设备收入增长强劲，服务业务本季度实现两位数增长，但利润率下滑。</p>"
        "<p>公司各地区的设备收入增长强劲，服务业务本季度实现两位数增长，并且利润率上升、需求稳定。</p>"
    ) + "".join(f"<p>第 {index} 行用平实的话说明分销成本。</p>" for index in range(1, 24))
    view = article_pairs(source, translated)
    assert view.notice == ""
    assert "falling" in _project(view.pairs, "en")
    assert content_gaps(source, _project(view.pairs, "en")) == 0


def test_soft_dedupe_that_would_lose_text_is_ignored(monkeypatch):
    """软去重是展示层：真收了会丢原文内容时，宁可整份细对都留着，也不整页分开。"""
    en, zh = _paragraphs(30)

    # 模拟一条过宽的去重判断：第 5 段带着独有文字，收掉就少内容。
    monkeypatch.setattr(display, "redundant_pairs", lambda pairs: [5], raising=False)
    view = article_pairs(en, zh)
    assert view.notice == ""
    assert len(view.pairs) == 30
    assert content_gaps(en, _project(view.pairs, "en")) == 0


def test_heading_and_repeated_occurrences_both_survive_display():
    """标题保留，完全相同的重复整对标记折叠而不删除。"""
    en, zh = _paragraphs(6)
    source = "<h2>Report</h2>" + en + _PARA_EN.format(index=3)
    translated = "<h2>报告</h2>" + zh + _PARA_ZH.format(index=3)
    view = article_pairs(source, translated)
    assert view.notice == ""
    assert "Report" in _project(view.pairs, "en")
    assert _project(view.pairs, "en").count("Paragraph 3 explains") == 2
    assert view.pairs[-1].repeat_of == 3
    assert content_gaps(source, _project(view.pairs, "en")) == 0


def _squash(value: str) -> str:
    from html import unescape

    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", value or "")))


def test_definition_citation_parentheses_stay_in_fine_pairs():
    """长定义里的来源注释被换行拆成「) (」。丢掉括号会让物化失败，整页退成两列。"""
    from researchlib.translate.provenance import materialize
    from researchlib.translate.segments import segments_of

    sentences = [
        f"Clause {name} explains distribution costs and the installed base for this quarter."
        for name in (
            "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
            "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
            "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
            "xray", "yankee", "zulu", "amber", "birch", "cedar", "dune",
        )
    ]
    body = "<br>".join([
        "Buyers were new to those devices (",
        "example.com",
        ") (",
        "example.com",
        ")). The note then explains the installed base.",
        *sentences,
        "As the note put it, the experience feels more personal.",
        "(",
        "example.com",
        ") It is a classic move that ties software to the hardware advantage.",
    ])
    source = f"<dl><dd>{body}</dd></dl>"
    assert len(_squash(source)) > 2000
    rendered = materialize(source)
    assert _squash(rendered) == _squash(source)
    leaves = segments_of(source)
    translated = "".join(
        "<p>这一句说明分销成本和已有用户，单独成对，不写数字。</p>" for _index in range(len(leaves))
    )
    view = article_pairs(source, translated)
    assert view.notice == ""
    assert len(view.pairs) == len(leaves) > 2
    shown = _squash(_project(view.pairs, "en"))
    assert "(example.com)(example.com)" in shown
    assert "personal.”(example.com)" in shown or "personal.(example.com)" in shown
    assert max(len(_squash(pair.en)) for pair in view.pairs) < 2000


def test_localized_numbers_do_not_collapse_equal_pairs():
    """300,000 写成 30万、100% 写成百分之百，不能把已经对齐的段落退成整页两列。"""
    facts = [
        ("Services revenue reached $300,000 in the holiday quarter and the base kept paying.",
         "服务收入在假日季度达到 30 万美元，已有用户继续付费。"),
        ("The success rate was 100% across every region in this short note.",
         "这篇短文里各地区的成功率是百分之百。"),
        ("China sales were $69.1 billion during the quarter and missed the prior forecast.",
         "该季度中国销售额为 691 亿美元，低于原先的预期。"),
        ("September 9, 2026 is the date written beside the foldable phone screenshot.",
         "折叠屏那张图旁边写的日期是 2026 年 9 月 9 日。"),
    ]
    filler = (
        "Distribution costs and the installed base stay in this sentence about the quarter.",
        "这句只讲分销成本和已有用户，没有需要换算的数字。",
    )
    en_bits = [en for en, _zh in facts] + [filler[0]] * 8
    zh_bits = [zh for _en, zh in facts] + [filler[1]] * 8
    source = "".join(f"<p>{bit}</p>" for bit in en_bits)
    translated = "".join(f"<p>{bit}</p>" for bit in zh_bits)
    view = article_pairs(source, translated)
    assert view.notice == ""
    assert len(view.pairs) == 12
    assert all(pair.en and pair.zh for pair in view.pairs)


def test_reordered_numeric_anchors_still_use_separate_columns():
    """同一批数字整段对调时，仍然不能按位置硬配。"""
    source = "".join(
        f"<p>Topic {index} explains the independent market mechanism and customer retention.</p>"
        for index in range(6)
    )
    translated = "".join(
        f"<p>议题{index}说明独立的市场机制和客户留存。</p>" for index in reversed(range(6))
    )
    view = article_pairs(source, translated)
    assert view.notice
    assert len(view.pairs) == 2
    assert all(not (pair.en and pair.zh) for pair in view.pairs)


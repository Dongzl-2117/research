"""按句切开译的长段并回一段：中文句子之间不加空格，英文照旧一个空格；「只看中文」不再一句一行。"""

import re

from researchlib.translate.display import _join_html, article_pairs, join_split_paragraphs
from researchlib.translate.provenance import source_manifest
from researchlib.translate.segments import iter_segments, splice_segments

PAD = (
    " and the rest of this sentence keeps going with more detail about the quarter"
    " so that the paragraph stays well past the split threshold"
)
EN = [
    "The airline rebuilt its app first because loyalty starts with booking",
    "Then it fixed the operating data so every delay could be traced",
    "Only after that did it change how managers talked about blame",
    "The result was a culture that cared about fixing problems",
    "That is why the customer experience improved so quickly",
]
ZH = [
    "这家航空公司先重做了 App，因为忠诚度从订票开始。",
    "接着它理顺了运营数据，每次延误都能追到原因。",
    "在那之后，它才改了经理们在会上谈责任的方式。",
    "结果形成了一种重在解决问题的文化。",
    "所以客户体验改善得很快。",
]
SOURCE = "<p>" + " ".join(sentence + PAD + "." for sentence in EN) + "</p><p>A short closing paragraph.</p>"


def _plain(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "")


def test_chinese_sentences_join_without_a_space_and_english_keeps_one():
    joined = _join_html([
        '<p data-src-para="1">第一句。</p>',
        '<p data-src-para="1">第二句，带 <a href="/x">Apple</a> 的链接。</p>',
    ])
    assert joined == '<p>第一句。第二句，带 <a href="/x">Apple</a> 的链接。</p>'
    # 弯引号英文里也用：英文句子以 ” 结尾，后面照旧空一格。
    english = _join_html(['<p data-src-para="2">He said “yes.”</p>', '<p data-src-para="2">Then he left.</p>'])
    assert english == "<p>He said “yes.” Then he left.</p>"
    # 中文句子以 ” 结尾、下一句以英文名开头：看引号里面那个字（「。」），直接接上。
    mixed = _join_html(["<p>他说：“好。”</p>", "<p>Apple 随后跟进。</p>"])
    assert mixed == "<p>他说：“好。”Apple 随后跟进。</p>"
    assert _join_html(["<p>他说：“好。”</p>", "<p>“Apple 也跟进了。”</p>"]) == "<p>他说：“好。”“Apple 也跟进了。”</p>"
    english_quotes = _join_html(["<p>(See the chart.)</p>", "<p>“Next,” he said.</p>"])
    assert english_quotes == "<p>(See the chart.) “Next,” he said.</p>"
    # 一句只有图、没有字：按前一句的末字判断。
    assert _join_html(["<p>上一句。</p>", '<p><img src="/media/a.png"></p>', "<p>下一句。</p>"]) == (
        '<p>上一句。<img src="/media/a.png">下一句。</p>'
    )


def test_pair_view_glues_chinese_sentences_of_one_source_paragraph():
    """对照栏：同一源段落按句译的几句并回一对，中文那一栏句号后面不再多一个空格（以前「。 接着」）。"""
    assert len(iter_segments(SOURCE)) == len(EN) + 1
    translated = "".join(f"<p>{line}</p>" for line in ZH) + "<p>收尾的短段落。</p>"
    view = article_pairs(SOURCE, translated)
    assert view.notice == ""
    merged = [pair for pair in view.pairs if "operating data" in (pair.en or "")]
    assert len(merged) == 1
    assert _plain(merged[0].zh) == "".join(ZH)
    assert ". Then it fixed" in _plain(merged[0].en)


def test_reading_view_joins_split_paragraphs_back():
    """「只看中文」：存下来的译文里，长段一句一个 <p data-src-para>；整篇读时并回一段，别的段落不动。"""
    snapshot = source_manifest(SOURCE)["source_snapshot"]
    leaves = iter_segments(snapshot)
    assert len(leaves) == len(EN) + 1
    assert all('data-src-para="' in leaf.html for leaf in leaves[: len(EN)])
    translated = splice_segments(snapshot, {
        index: f"<p>{line}</p>" for index, line in enumerate([*ZH, "收尾的短段落。"])
    })
    assert translated.count("data-src-para") == len(EN)
    reading = join_split_paragraphs(translated)
    assert reading == "<p>" + "".join(ZH) + "</p><p>收尾的短段落。</p>"


def test_reading_view_only_joins_adjacent_pieces_of_the_same_paragraph():
    translated = (
        '<p data-src-para="1">甲一。</p><p data-src-para="1">甲二。</p>'
        "<p>中间一段没有标记。</p>"
        '<p data-src-para="1">编号相同、但不挨着。</p>'
        '<p data-src-para="2">乙一。</p>\n<p data-src-para="2">乙二。</p>'
        '<ul><li><p data-src-para="3">列表里第一句。</p><p data-src-para="3"><strong>第二句</strong>。</p></li></ul>'
        '<p data-src-para="4">只有一句。</p>'
        "<p>第一行<br>第二行<br>第三行</p>"
    )
    assert join_split_paragraphs(translated) == (
        "<p>甲一。甲二。</p>"
        "<p>中间一段没有标记。</p>"
        "<p>编号相同、但不挨着。</p>"
        "<p>乙一。乙二。</p>"
        "<ul><li><p>列表里第一句。<strong>第二句</strong>。</p></li></ul>"
        "<p>只有一句。</p>"
        "<p>第一行<br>第二行<br>第三行</p>"
    )
    assert join_split_paragraphs("") == ""

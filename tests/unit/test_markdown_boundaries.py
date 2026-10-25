"""Literal blanks and HTML void elements must not erase prose boundaries."""

import re

import pytest

from researchlib.ingest.store import _body_text
from researchlib.mdlinks import prepare_body_html
from researchlib.textutil import markdown_to_text
from researchlib.translate.pair import bilingual_pairs, stamp_paragraph_ids
from researchlib.translate.segments import top_level_blocks
from researchlib.web.quoting import mark_quoted


@pytest.mark.parametrize("text", [
    "当我_______后，我会________。",
    "当我___后，我会____。",
    "姓名：________；日期：________。",
    "snake_case and an_unmatched_marker",
    r"转义的\_字面\_与\_\_强调示例\_\_。",
])
def test_literal_underscores_survive_plain_text_and_ingest(text):
    assert markdown_to_text(text) == text
    assert _body_text("示例", text) == text


@pytest.mark.parametrize("code", ["__init__", "_literal_", "snake_case"])
@pytest.mark.parametrize("ticks", ["`", "``"])
def test_inline_code_keeps_literal_underscores_while_prose_emphasis_is_removed(code, ticks):
    source = f"调用 {ticks}{code}{ticks}，另有 _强调_。"
    assert markdown_to_text(source) == f"调用 {code}，另有 强调。"


def test_double_backtick_code_can_contain_single_backtick():
    assert markdown_to_text("调用 ``value`_literal_``。") == "调用 value`_literal_。"


def test_fenced_code_is_still_excluded_and_inline_blanks_are_literal():
    fenced = markdown_to_text("前文\n\n```text\n__excluded__\n```\n\n后文")
    assert fenced.split() == ["前文", "后文"]
    assert "excluded" not in fenced and "```" not in fenced
    assert markdown_to_text("`___`") == "___"


@pytest.mark.parametrize(("source", "expected"), [
    ("_斜体_ 与 __强调__ 和 ___双重___。", "斜体 与 强调 和 双重。"),
    ("__outer _inner_ outer__", "outer inner outer"),
    ("_first\nsecond_ 与 __two\nlines__", "first\nsecond 与 two\nlines"),
    ("__outer _inner\nline_ outer__", "outer inner\nline outer"),
    ("> __引用强调__。", "引用强调。"),
    ("上文\n\n___\n\n下文", "上文\n\n下文"),
    ("上文\n\n_ _ _\n\n下文", "上文\n\n下文"),
    ("> ___\n\n后文", "后文"),
    ("上文\n\n***\n\n下文", "上文\n\n下文"),
])
def test_real_emphasis_and_thematic_rules_are_still_removed(source, expected):
    assert markdown_to_text(source) == expected


@pytest.mark.parametrize("void", ["br", "hr", "img", "wbr", "input", "source", "track", "col", "embed"])
@pytest.mark.parametrize("ending", [">", " />"])
def test_void_elements_do_not_consume_following_top_level_blocks(void, ending):
    middle = f"<blockquote><p>First<{void}{ending}second</p></blockquote>"
    html = "<p>Before.</p>" + middle + "<p>After.</p>"
    blocks = top_level_blocks(html)
    assert len(blocks) == 3
    assert blocks[1].startswith("<blockquote>") and blocks[1].endswith("</blockquote>")
    assert blocks[2] == "<p>After.</p>"
    stamped = stamp_paragraph_ids(html)
    assert '<blockquote id="p-2">' in stamped
    assert '<p id="p-3">After.</p>' in stamped
    assert stamp_paragraph_ids(stamped) == stamped


def test_void_blocks_nested_lists_and_pre_code_keep_real_structure():
    html = '<hr><ul><li>A<br>B<ul><li>C<img src="/x.png"></li></ul></li></ul><pre><code>x<br>y</code></pre><p>End.</p>'
    blocks = top_level_blocks(html)
    assert len(blocks) == 4
    assert blocks[0] == "<hr>"
    assert blocks[1].endswith("</ul>")
    assert blocks[2] == "<pre><code>x<br>y</code></pre>"
    assert blocks[3] == "<p>End.</p>"


def test_real_reader_chain_anchors_after_quote_and_does_not_invent_citations():
    later = "A later paragraph supplies independently identifiable evidence about attention."
    body = "Before.\n\n> First quoted line.\n> Second quoted line.\n\n" + later
    html = prepare_body_html(body, "Example", {}, None, None)
    assert '<p id="p-3">' + later + "</p>" in html
    marked, found = mark_quoted(html, later)
    assert found and '<p id="p-3" class="is-quoted">' + later in marked
    assert mark_quoted(html, "Absent evidence that the source never supplied.") == (html, False)
    assert marked.count("is-quoted") == 1
    assert len(re.findall(r'id="p-\d+"', html)) == 3


def test_bilingual_pairs_still_align_after_multiline_quote():
    original = stamp_paragraph_ids("<p>Before.</p><blockquote><p>One<br>two.</p></blockquote><p>After.</p>")
    translated = stamp_paragraph_ids("<p>之前。</p><blockquote><p>一<br>二。</p></blockquote><p>之后。</p>")
    pairs = bilingual_pairs(original, translated)
    assert pairs is not None and len(pairs) == 3
    assert [pair.anchor for pair in pairs] == ["p-1", "p-2", "p-3"]
    assert pairs[-1].en == "<p>After.</p>" and pairs[-1].zh == "<p>之后。</p>"


def test_nested_quotes_keep_outer_boundary_and_native_ids_are_idempotent():
    quote = '<blockquote><p>Outer<br>line</p><blockquote><p>Inner<br>line</p></blockquote></blockquote>'
    html = quote + '\n\n<p id="native-target">After.</p>'
    blocks = top_level_blocks(html)
    assert len(blocks) == 2 and blocks[0] == quote
    stamped = stamp_paragraph_ids(html)
    assert '<blockquote id="p-1">' in stamped
    assert '<p id="p-2"><span id="native-target" class="para-anchor"></span>After.</p>' in stamped
    assert stamp_paragraph_ids(stamped) == stamped
    assert stamped.count('id="native-target"') == 1


def test_soft_breaks_do_not_create_paragraphs_or_duplicate_text():
    html = prepare_body_html("First line.\nSecond line.\n\nLast paragraph.", "Example", {}, None, None)
    assert len(top_level_blocks(html)) == 2
    assert html.count('id="p-') == 2
    assert html.count("First line.") == html.count("Second line.") == html.count("Last paragraph.") == 1

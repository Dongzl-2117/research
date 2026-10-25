"""Textual location covers the complete excerpt, without asserting entailment."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from researchlib.citecheck import check_answer, check_claim, quote_has_omission, quote_spans
from researchlib.mdlinks import prepare_body_html
from researchlib.web.quoting import mark_quoted


@pytest.mark.parametrize("source,quote", [
    ("经营收入增长，利润仍需观察。", "经营收入增长 利润仍需观察"),
    ("The company’s revenue grew, but risks remain.", "THE COMPANY'S REVENUE GREW BUT RISKS REMAIN"),
    ("Revenue reached 1,200.5% in this example.", "revenue reached 1200.5% in this example"),
    ("收益增长１．２％，仍需观察", "收益增长1.2%，仍需观察"),
    ("收益变化为－5%，仍需观察", "收益变化为-5%，仍需观察"),
    ("收益变化为＋5%，仍需观察", "收益变化为+5%，仍需观察"),
    ("The C++ language remains useful.", "the C++ language remains useful"),
    ("Alpha is supported.\nBeta remains uncertain.", "alpha is supported beta remains uncertain"),
])
def test_complete_text_allows_typographic_differences(source, quote):
    assert quote_spans(source, quote)


@pytest.mark.parametrize("source,quote", [
    ("收益变化为-5%，仍需观察", "收益变化为5%，仍需观察"),
    ("收益变化为5%，仍需观察", "收益变化为-5%，仍需观察"),
    ("收益变化为1.2%，仍需观察", "收益变化为12%，仍需观察"),
    ("收益变化为12%，仍需观察", "收益变化为1.2%，仍需观察"),
    ("收益变化为5%，仍需观察", "收益变化为5，仍需观察"),
    ("The C++ language remains useful.", "The C language remains useful."),
    ("The C language remains useful.", "The C++ language remains useful."),
    ("This outcome is notable and uncertain.", "This outcome is not able and uncertain."),
    ("These are separate words in this example.", "These are separatewords in this example."),
    ("The value a+b remains uncertain.", "The value ab remains uncertain."),
    ("公式：成本=售价-利润，仍需核对", "公式：成本=售价利润，仍需核对"),
    ("收益≤5%，仍需观察", "收益≥5%，仍需观察"),
    ("The value a×b remains uncertain.", "The value a÷b remains uncertain."),
    ("成本$5，仍需观察", "成本€5，仍需观察"),
    ("收益－5%，仍需观察", "收益5%，仍需观察"),
    ("收益＋5%，仍需观察", "收益5%，仍需观察"),
    ("The first sentence is supported by this source.", "The first sentence is supported by this source and audited."),
])
def test_semantic_tokens_and_complete_tail_are_required(source, quote):
    assert quote_spans(source, quote) is None


@pytest.mark.parametrize("separator", ["\u00a0", "\u202f"])
@pytest.mark.parametrize("grouped,plain", [("1{sep}200.5", "1200.5"), ("-1{sep}200.5%", "-1200.5%"),
                                          ("1{sep}200{sep}000", "1200000")])
def test_strict_nonbreaking_thousands_groups_are_typographic(separator, grouped, plain):
    source = f"The value was {grouped.format(sep=separator)} dollars in this example."
    quote = f"The value was {plain} dollars in this example."
    assert quote_spans(source, quote)
    assert mark_quoted(f"<p>{source}</p>", quote)[1]
    assert not _quote_problems(quote, [{"n": 1, "text": source}], [1])


@pytest.mark.parametrize("grouped,plain", [
    ("1 200.5", "1200.5"), ("1\n200.5", "1200.5"), ("1\u202f20.5", "120.5"),
    ("1\u00a020.5", "120.5"), ("1\u202f200.5%", "1200.5"), ("-1\u202f200.5", "1200.5"),
])
def test_group_normalization_cannot_merge_words_or_change_units_and_signs(grouped, plain):
    source = f"The value was {grouped} dollars in this example."
    quote = f"The value was {plain} dollars in this example."
    assert quote_spans(source, quote) is None
    assert mark_quoted(f"<p>{source}</p>", quote)[1] is False
    assert _quote_problems(quote, [{"n": 1, "text": source}], [1])


def test_prefix_is_not_a_complete_quote_even_after_a_long_shared_prefix():
    source = "这段真实文字已超过二十四个字，讨论订单变化以及观察数据的限制，尚不能确定未来。"
    html = str(prepare_body_html(source, "", {}, None, None))
    assert mark_quoted(html, source)[1]
    assert mark_quoted(html, source[:30] + "因此审计已经证明未来结果必然成立。") == (html, False)
    assert mark_quoted(html, source + "最后这个结论已经得到确定证明。") == (html, False)


@pytest.mark.parametrize("omission", ["…", "……", "...", "[省略]", "（略）"])
def test_only_explicit_omission_can_skip_unquoted_source_text(omission):
    html = ('<p id="p-1">Opening evidence is present.</p><p id="p-2">Intervening uncertainty remains.</p>'
            '<p id="p-3">Final evidence is present.</p>')
    full = "Opening evidence is present. Final evidence is present."
    assert mark_quoted(html, full) == (html, False)
    quote = f"Opening evidence is present.{omission}Final evidence is present."
    marked, found = mark_quoted(html, quote)
    assert found and quote_has_omission(quote)
    assert marked.count("is-quoted") == 2
    assert '<p id="p-2">' in marked
    assert mark_quoted(html, quote + " This tail is fabricated.") == (html, False)
    assert mark_quoted(html, f"Final evidence is present.{omission}Opening evidence is present.") == (html, False)


def test_adjacent_paragraphs_can_form_one_complete_excerpt():
    source = "Opening evidence is present.\n\nFinal evidence is present."
    html = str(prepare_body_html(source, "", {}, None, None))
    marked, found = mark_quoted(html, source)
    assert found and marked.count("is-quoted") == 2


def test_repeated_paragraphs_choose_one_complete_occurrence_and_are_idempotent():
    target = "The complete statement is long enough and includes its real final clause."
    html = f"<p>The complete statement is long enough but unrelated.</p><p>{target}</p><p>{target}</p>"
    marked, found = mark_quoted(html, target)
    assert found and marked.count("is-quoted") == 1
    assert f'<p class="is-quoted">{target}</p><p>{target}</p>' in marked
    assert mark_quoted(marked, target) == (marked, True)


@pytest.mark.parametrize("target", ["", "短句", "…", "No evidence for this complete statement."])
def test_missing_or_insufficient_evidence_is_not_marked(target):
    html = "<p>A different paragraph is present in the source.</p>"
    assert mark_quoted(html, target) == (html, False)


def _quote_problems(quote, citations, refs=None):
    problems = check_claim(f'作者说“{quote}”[1][2]。', refs or [1, 2], citations, [])
    return [problem for problem in problems if "引号里" in problem]


def test_direct_quote_cannot_be_assembled_from_different_citations():
    citations = [
        {"n": 1, "article_id": 1, "text": "Opening evidence is present."},
        {"n": 2, "article_id": 2, "text": "Final evidence is present."},
    ]
    assert _quote_problems("Opening evidence is present. Final evidence is present.", citations)
    assert _quote_problems("Opening evidence is present…Final evidence is present.", citations)
    # Even a shared article ID does not prove the omitted intervening context/order.
    citations[1]["article_id"] = 1
    assert _quote_problems("Opening evidence is present…Final evidence is present.", citations)


def test_direct_quote_can_use_one_citation_context_and_repeat_safely():
    citation = {"n": 1, "before": "Opening evidence is present.", "text": "Intervening uncertainty remains.",
                "after": "Final evidence is present."}
    quote = "Opening evidence is present…Final evidence is present."
    assert not _quote_problems(quote, [citation], [1])
    assert not _quote_problems(quote, [citation, {**citation, "n": 2}])
    assert _quote_problems("Opening evidence is present. Final evidence is present.", [citation], [1])


def test_metadata_cannot_supply_a_missing_direct_quote():
    citation = {"n": 1, "title": "Opening evidence is present.", "heading": "Final evidence is present.",
                "text": "A different source paragraph."}
    assert _quote_problems("Opening evidence is present.", [citation], [1])


@pytest.mark.parametrize("opening,closing", [("“", "”"), ("「", "」"), ('"', '"')])
def test_answer_checks_the_whole_multisentence_quote(opening, closing):
    citation = {"n": 1, "text": "尾句确实来自原文，风险仍需观察。"}
    answer = f"作者说{opening}这里凭空加入保证盈利的承诺。尾句确实来自原文，风险仍需观察。{closing}[1]"
    flags = check_answer(answer, [citation], [])
    assert any("引号里" in problem for flag in flags for problem in flag["problems"])
    faithful = f"作者说{opening}{citation['text']}{closing}[1]"
    assert not check_answer(faithful, [citation], [])


def test_answer_does_not_join_two_citations_for_a_direct_quote():
    answer = "作者说“开头确实来自第一段。尾句确实来自第二段。”[1][2]"
    citations = [{"n": 1, "text": "开头确实来自第一段。"}, {"n": 2, "text": "尾句确实来自第二段。"}]
    flags = check_answer(answer, citations, [])
    assert any("引号里" in problem for flag in flags for problem in flag["problems"])


@pytest.mark.parametrize("source,quote", [
    ("The value was -5% in this report.", "The value was 5% in this report."),
    ("The value was 1.2 in this report.", "The value was 12 in this report."),
    ("The C++ language remains useful.", "The C language remains useful."),
    ("收益≤5%，仍需观察", "收益≥5%，仍需观察"),
    ("The value a×b remains uncertain.", "The value a÷b remains uncertain."),
    ("成本$5，仍需观察", "成本€5，仍需观察"),
    ("收益－5%，仍需观察", "收益5%，仍需观察"),
    ("公式：成本=售价-利润，仍需核对", "公式：成本=售价利润，仍需核对"),
])
def test_direct_quote_check_uses_the_same_semantic_boundaries(source, quote):
    assert _quote_problems(quote, [{"n": 1, "text": source}], [1])


def _templates():
    root = Path(__file__).parents[2] / "src/researchlib/web/templates"
    env = Environment(loader=FileSystemLoader(root), autoescape=select_autoescape(["html"]))
    env.filters["highlight_snippet"] = lambda text, *_: text
    env.filters["highlight_full"] = lambda text, *_: text
    env.filters["answer_html"] = lambda text: text
    return env


@pytest.mark.parametrize("found,omitted,expected", [
    (False, False, "未能在当前正文中完整定位"),
    (True, False, "已在正文中定位所引片段"),
    (True, True, "省略部分未核对"),
])
def test_article_states_textual_location_without_claiming_support(found, omitted, expected):
    html = _templates().get_template("article.html").render(
        article={"id": 1, "title": "Example"}, source={}, sources=[], siblings=[], pairs=[],
        quoted={"heading": "", "body": "A supplied excerpt."},
        quoted_marked=found, quoted_omitted=omitted,
    )
    assert expected in html
    if found:
        assert "不代表回答的整合或推断已获证" in html
    else:
        assert "待核对的引用片段" in html and "引用的段落" not in html


def test_answer_without_rule_warnings_still_explains_inference_limit():
    html = _templates().get_template("ask.html").render(
        sources=[], filters={}, result={"answer": "A synthesized answer.", "citations": [], "checks": []},
    )
    assert "推断应明确标注" in html
    assert "都不等于结论已核实" in html

"""审计 5–7：错误回链、显式文章范围、分批身份与约束。全为合成材料。"""
from datetime import date
from html import escape

import pytest

from researchlib import ask
from researchlib.retrieval import Passage, Query, _scope_for_question
from researchlib.sources import Source
from researchlib.web.queries import Scope
from researchlib.web.quoting import mark_quoted

PREFIX = "公司年报复盘关注渠道费用与经营现金流的持续变化以及利润结构，"


@pytest.mark.parametrize("wrong,right", [
    (PREFIX + "利润增长90%。", PREFIX + "利润下降80%。"),
    (PREFIX + "利润为+8%。", PREFIX + "利润为-8%。"),
    (PREFIX + "利润为15亿元。", PREFIX + "利润为1.5亿元。"),
    (PREFIX + "利润为8亿元。", PREFIX + "利润为8%。"),
    (PREFIX + "利润>10%。", PREFIX + "利润<10%。"),
    (PREFIX + "利润≥10%。", PREFIX + "利润≤10%。"),
    (PREFIX + "利润=10%。", PREFIX + "利润≠10%。"),
    (PREFIX + "比例为1/23。", PREFIX + "比例为12/3。"),
    (PREFIX + "比例为1:23。", PREFIX + "比例为12:3。"),
    (PREFIX + "收入为10m。", PREFIX + "收入为10M。"),
    (PREFIX + "计算值为10²。", PREFIX + "计算值为102。"),
    (PREFIX + "编码为1 23。", PREFIX + "编码为12 3。"),
])
def test_quote_checks_entire_text_and_numeric_meaning(wrong, right):
    html = f"<p>{escape(wrong)}</p><p>{escape(right)}</p>"
    marked, found = mark_quoted(html, right)
    assert found and marked.count('class="is-quoted"') == 1
    assert f'<p class="is-quoted">{escape(right)}</p>' in marked
    wrong_html = f"<p>{escape(wrong)}</p>"
    assert mark_quoted(wrong_html, right) == (wrong_html, False)


@pytest.mark.parametrize("tail", ["缺失的尾段，利润下降80%。", "共同前缀后面完全不同。"])
def test_partial_quote_does_not_hide_accurate_fallback(tail):
    body = f"<p>{PREFIX}</p><p>利润增长90%。</p>"
    assert mark_quoted(body, PREFIX + tail) == (body, False)


def test_repeated_full_quote_is_ambiguous_and_keeps_fallback():
    body = f"<p>{PREFIX}</p><p>另一节。</p><p>{PREFIX}</p>"
    assert mark_quoted(body, PREFIX) == (body, False)


def test_quote_crosses_real_paragraphs_and_checks_last_partial_paragraph():
    end = "利润下降80%，由于渠道费用上升。"
    html = f"<p>导语。{PREFIX}</p><p>{end}余下内容。</p>"
    marked, found = mark_quoted(html, PREFIX + end)
    assert found and marked.count('class="is-quoted"') == 2
    assert mark_quoted(html, PREFIX + end.replace("下降", "增长")) == (html, False)


def test_explicit_article_scope_is_not_replaced_by_inferred_author():
    sources = [Source("a", "札记甲", "王小明", "markdown", True), Source("b", "札记乙", "李四", "markdown", True)]
    query = Query("李四的判断", [], [], ["李四"], [], [], [], False, True, False, False)
    article_scope = Scope(source_ids=["a", "b"], article_id=7)
    kept = _scope_for_question(article_scope, query, sources)
    assert kept == article_scope
    library = _scope_for_question(Scope(source_ids=["a", "b"]), query, sources)
    assert library.source_ids == ["b"] and library.article_id is None


def test_live_scope_drops_revoked_sources_and_keeps_other_bounds():
    scope = Scope(
        source_ids=["a", "b"], article_id=7, company_id=3, tree_id=9,
        date_from=date(2026, 1, 1), date_to=date(2026, 1, 1),
    )
    live = [Source("b", "札记乙", "李四", "markdown", True)]
    kept = ask._live_scope(scope, live)
    assert kept.source_ids == ["b"]
    assert (kept.article_id, kept.company_id, kept.tree_id, kept.date_from, kept.date_to) == (
        7, 3, 9, date(2026, 1, 1), date(2026, 1, 1),
    )
    assert ask._live_scope(scope, []).source_ids == []
    assert ask._live_scope(scope, [
        Source("a", "札记甲", "王小明", "markdown", True),
        Source("b", "札记乙", "李四", "markdown", True),
    ]) == scope


@pytest.mark.parametrize("batched", [False, True])
def test_article_identity_and_constraints_survive_all_synthesis_steps(monkeypatch, batched):
    items = [
        {"n": 1, "title": "同名文章", "author": "王小明", "source": "札记", "date": "2026-01-01",
         "heading": "机制", "text": "渠道收入。" * 30, "place_label": "当前文章"},
        {"n": 2, "title": "同名文章", "author": "王小明", "source": "札记", "date": "2026-01-01",
         "heading": "成本", "text": "渠道成本。" * 30, "place_label": "库里其他文章"},
    ]
    monkeypatch.setattr(ask, "SINGLE_CALL_CHARS", 100 if batched else 40000)
    monkeypatch.setattr(ask, "BATCH_CHARS", 250)
    calls = []

    def chat(settings, messages, **kwargs):
        calls.append(messages)
        return "摘录故意省略身份 [1][2]", "synthetic"

    monkeypatch.setattr(ask, "chat", chat)
    note = "资料只覆盖了列出的章节。"
    system = "只依据给出的资料作答。"
    ask.synthesize(None, "概括当前文章", items, note=note, system=system)
    assert len(calls) == (3 if batched else 1)
    for messages in calls:
        assert system in messages[0]["content"]
        assert note in messages[1]["content"]
    final = calls[-1][1]["content"]
    assert "[1] 《同名文章》 王小明 · 札记 · 2026-01-01 · 章节：机制 · 当前文章" in final
    assert "[2] 《同名文章》 王小明 · 札记 · 2026-01-01 · 章节：成本 · 库里其他文章" in final
    if batched:
        for label in ("当前文章", "库里其他文章"):
            assert any(label in m[1]["content"] for m in calls[:-1])


def test_ordinary_question_material_still_has_no_sidebar_identity():
    passage = Passage(1, 0, "", "普通问库资料。", "资料", "a", None)
    source = Source("a", "札记", "王小明", "markdown", True)
    item = ask._passage_dict(1, passage, {"a": source}, "Asia/Shanghai")
    assert "当前文章" not in ask._material([item])
    assert "库里其他文章" not in ask._material([item])

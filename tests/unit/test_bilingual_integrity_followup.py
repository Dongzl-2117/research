"""数字、脚注、正式身份协议重试和受保护代码的新增审计回归。"""

import re

import pytest

from researchlib.translate import provenance as pr
from researchlib.translate import service
from researchlib.translate.display import article_pairs
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.qa import report_from_html
from researchlib.translate.segments import shield, unshield


@pytest.mark.parametrize(("source", "target", "numbers"), [
    ("Q3 revenue in FY2026 was 10 dollars.", "3季度（2026财年）收入为10美元。", ["3", "2026", "10"]),
    ("FY2026 Q3 revenue was 10 dollars.", "2026财年3季度收入为10美元。", ["2026", "3", "10"]),
    ("H100 costs 1,200.5 dollars at -2.5%.", "H100价格为1200.5美元，变化为-2.5%。", ["100", "1200.5", "-2.5%"]),
])
def test_embedded_financial_and_product_numbers_are_checked(source, target, numbers):
    assert pr.numeric_anchors(source) == numbers
    assert pr.leaf_problem(source, target) is None
    changed = source.replace("Q3", "Q4").replace("FY2026", "FY2027").replace("H100", "H200")
    assert pr.leaf_problem(source, changed) == "numbers"


def test_correct_embedded_numbers_can_receive_and_verify_a_receipt():
    source = "<p>FY2026 Q3 revenue was 10 dollars.</p>"
    target = "<p>2026财年3季度收入为10美元。</p>"
    proof = pr.receipt(pr.source_manifest(source), target)
    assert pr.verify(source, target, proof).status == "mapped"


@pytest.mark.parametrize("quarter", ["第三季度", "三季度"])
def test_chinese_financial_quarter_words_keep_the_same_numeric_identity(quarter):
    source = '<p>FY2026 Q3 revenue was 10 dollars.</p>'
    target = f'<p>2026财年{quarter}收入为10美元。</p>'
    assert pr.leaf_problem(source, target) is None
    assert pr.verify(source, target, pr.receipt(pr.source_manifest(source), target)).status == "mapped"
    assert pr.leaf_problem(source, target.replace('三季度', '四季度')) == "numbers"


def test_numeric_mismatch_is_not_hidden_by_a_simultaneous_link_mismatch():
    source = '<p>Revenue was 10 dollars. <a href="https://example.invalid/a">Report</a></p>'
    target = '<p>收入为20美元。<a href="https://example.invalid/b">报告</a></p>'
    view = article_pairs(source, target)
    assert view.notice
    assert all(not (item.en and item.zh) for item in view.pairs)
    assert "example.invalid/a" in view.pairs[0].en
    assert "example.invalid/b" in (view.pairs[1].zh or "")
    assert report_from_html(1, "synthetic", "合成报告", source, target).status == "fail"


def test_named_footnote_anchor_and_fragment_link_survive_restoration():
    source = '<p><a id="note-1" name="old-note" href="#back-1">Footnote 1</a></p>'
    protected = shield(source)
    target = unshield(protected.html.replace("Footnote", "脚注"), protected)
    assert target is not None
    assert 'id="note-1"' in target and 'name="old-note"' in target
    assert 'href="#back-1"' in target
    assert not re.search(r'id="L\d+"', target)


def test_link_restoration_rejects_extra_or_duplicate_temporary_ids():
    protected = shield('<p><a href="#note-1">Note</a></p>')
    assert unshield(protected.html + '<a id="L0">额外链接</a>', protected) is None
    assert unshield(protected.html + '<a id="L9">未知链接</a>', protected) is None


@pytest.mark.parametrize("mode", ["repair", "split"])
def test_formal_identity_count_failures_reach_repair_and_split(monkeypatch, tmp_path, mode):
    parts = [f"<p>Paragraph {i}.</p>" for i in range(4)]
    context = {"segment_ids": [f"source-{i}" for i in range(4)],
               "source_hashes": [pr.digest(part) for part in parts]}
    calls = []

    def fake(**kwargs):
        user = kwargs["user"]
        calls.append((len(user["segments"]), "repair" in user))
        for part, identity, digest in zip(user["segments"], user["segment_ids"], user["source_hashes"], strict=True):
            index = parts.index(part)
            assert identity == context["segment_ids"][index] and digest == context["source_hashes"][index]
        rows = [{"id": identity, "source_hash": digest, "html": part}
                for part, identity, digest in zip(user["segments"], user["segment_ids"],
                                                   user["source_hashes"], strict=True)]
        if len(rows) > 1 and (mode == "split" or "repair" not in user):
            rows = rows[:-1]
        return ChatJsonResult(data={"t": rows}, model="synthetic", usage=None, latency_ms=0)

    monkeypatch.setattr(service, "chat_json", fake)
    result = service._translate_batch(parts, context, tmp_path)
    assert result == parts and result.models
    assert calls[:2] == [(4, False), (4, True)]
    assert (len(calls) == 2) if mode == "repair" else any(size == 1 for size, _ in calls)


def test_count_repair_cannot_bypass_response_identity_checks(monkeypatch, tmp_path):
    context = {"segment_ids": ["first", "second"], "source_hashes": ["a", "b"]}
    calls = []

    def fake(**kwargs):
        calls.append(kwargs["user"])
        rows = [] if len(calls) == 1 else [
            {"id": "unknown", "source_hash": "a", "html": "甲"},
            {"id": "second", "source_hash": "b", "html": "乙"},
        ]
        return ChatJsonResult(data={"t": rows}, model="synthetic", usage=None, latency_ms=0)

    monkeypatch.setattr(service, "chat_json", fake)
    with pytest.raises(pr.AlignmentError, match="response-id-order"):
        service._translate_batch(["甲", "乙"], context, tmp_path)
    assert len(calls) == 2


def test_long_protected_inline_code_does_not_count_as_untranslated_english():
    body = "<p>示例：<code>" + "synthetic_function() " * 12 + "</code></p>"
    report = report_from_html(1, "synthetic", "合成代码", body, body)
    assert not report.long_empty_zh
    assert report.status == "warn"


def test_ignoring_code_does_not_hide_missing_english_prose_or_code():
    source = "<p>" + "This is a long untranslated explanation about customer retention. " * 3
    source += "<code>synthetic()</code></p>"
    assert report_from_html(1, "synthetic", "合成代码", source, source).long_empty_zh
    code = "<p>示例：<code>synthetic_function()</code></p>"
    report = report_from_html(1, "synthetic", "合成代码", code, "<p>示例。</p>")
    assert "protected-content-loss" in {item.code for item in report.issues}

"""处理版本、出现位置和模型返回身份的核验。"""

import copy

import pytest

from researchlib.translate import service
from researchlib.translate.display import content_gaps
from researchlib.translate.integrity import (
    make_provenance,
    provenance_problem,
    segment_problem,
    source_manifest,
)
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.qa import report_from_html
from researchlib.translate.segments import shield, unshield

EN = "<p>Alpha earned 10 dollars.</p><p>Beta spent 20 dollars.</p><p>Gamma held 30 dollars.</p>"
ZH = "<p>甲赚了10美元。</p><p>乙花了20美元。</p><p>丙持有30美元。</p>"


@pytest.mark.parametrize("bad_ids", [None, [], ["unknown"], ["duplicate", "duplicate", "duplicate"], "ids"])
def test_missing_duplicate_unknown_and_non_array_ids_fail(monkeypatch, tmp_path, bad_ids):
    def fake(**kwargs):
        return ChatJsonResult(data={"ids": bad_ids, "t": ["甲", "乙", "丙"]},
                              model="synthetic", usage=None, latency_ms=0)

    monkeypatch.setattr(service, "chat_json", fake)
    with pytest.raises(RuntimeError, match="segment IDs"):
        service._translate_batch(["<p>Alpha</p>", "<p>Beta</p>", "<p>Gamma</p>"], {}, tmp_path)


def test_reordered_ids_fail_even_when_count_is_equal(monkeypatch, tmp_path):
    def fake(**kwargs):
        return ChatJsonResult(data={"ids": list(reversed(kwargs["user"]["segmentIds"])), "t": ["乙", "甲"]},
                              model="synthetic", usage=None, latency_ms=0)

    monkeypatch.setattr(service, "chat_json", fake)
    with pytest.raises(RuntimeError, match="segment IDs"):
        service._translate_batch(["<p>Alpha</p>", "<p>Beta</p>"], {}, tmp_path)


def test_invalid_request_id_list_is_not_silently_regenerated(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "chat_json", lambda **kwargs: pytest.fail("无效身份不能送模型"))
    with pytest.raises(RuntimeError, match="invalid request segment IDs"):
        service._translate_batch(["<p>Alpha</p>"], {"segmentIds": []}, tmp_path)


def test_good_ids_do_not_hide_swapped_numeric_contents(monkeypatch, tmp_path):
    def fake(**kwargs):
        return ChatJsonResult(data={"ids": kwargs["user"]["segmentIds"],
                                    "t": ["<p>甲赚了10美元。</p>", "<p>丙持有30美元。</p>", "<p>乙花了20美元。</p>"]},
                              model="synthetic", usage=None, latency_ms=0)

    monkeypatch.setattr(service, "chat_json", fake)
    with pytest.raises(RuntimeError, match="numbers"):
        service._translate_batch(["<p>Alpha earned 10 dollars.</p>", "<p>Beta spent 20 dollars.</p>",
                                  "<p>Gamma held 30 dollars.</p>"], {}, tmp_path)


def test_repeat_occurrences_have_distinct_ordered_identities():
    manifest = source_manifest(EN + EN)
    assert len({item["id"] for item in manifest}) == 6
    assert [item["occurrence"] for item in manifest] == [0, 0, 0, 1, 1, 1]
    assert manifest[0]["sourceHash"] == manifest[3]["sourceHash"]


def test_canonical_cleaning_changes_are_visible_even_with_unchanged_raw_body():
    provenance = make_provenance(EN, ZH)
    assert provenance_problem(EN, ZH, provenance) is None
    changed = EN.replace("<p>Beta spent 20 dollars.</p>", "")
    assert provenance_problem(changed, ZH, provenance) == "canonical-input-changed"
    assert provenance_problem(EN, ZH, None) == "legacy-unverified"


def test_version_output_and_manifest_changes_are_rejected():
    provenance = make_provenance(EN, ZH)
    changed = copy.deepcopy(provenance)
    changed["version"]["cleaner"] += 1
    assert provenance_problem(EN, ZH, changed) == "processing-version"
    assert provenance_problem(EN, ZH.replace("甲", "丁"), provenance) == "translation-changed"
    for mutate in (lambda rows: rows.pop(), lambda rows: rows.append(rows[0]), lambda rows: rows.reverse()):
        changed = copy.deepcopy(provenance)
        mutate(changed["segments"])
        assert provenance_problem(EN, ZH, changed) == "manifest-invalid"


def test_fact_ownership_multiplicity_and_code():
    left = '<p>Read <a href="https://example.invalid/one">the report</a> at 10:10.</p>'
    assert segment_problem(left, left.replace("/one", "/two")) == "resources"
    assert segment_problem(left, left.replace("10:10", "10")) == "numbers"
    assert segment_problem("<p>Use <code>alpha()</code>.</p>", "<p>用<code>beta()</code>。</p>") == "code"
    first, second = '<img src="/media/a.png">', '<img src="/media/b.png">'
    assert content_gaps(first + second, second + first) > 0


@pytest.mark.parametrize("link", ['<a id="L0">甲</a><a id="L0">乙</a>', '<a id="L1">甲</a>', "甲"])
def test_duplicate_unknown_missing_link_tokens_are_rejected(link):
    protected = shield('<p>See <a href="https://example.invalid/one">the report</a>.</p>')
    assert unshield(f"<p>{link}</p>", protected) is None


def test_legacy_equal_counts_are_warned_instead_of_semantic_pass():
    report = report_from_html(1, "synthetic", "合成报告", EN, ZH)
    assert report.status == "warn"
    assert report.mapping_status == "legacy-unverified"
    assert "mapping-unverified" in {issue.code for issue in report.issues}
    assert report.as_dict()["semanticReview"] == "not-reviewed"


def test_swapped_legacy_rows_fail_mapping_even_with_no_content_loss():
    swapped = "<p>甲赚了10美元。</p><p>丙持有30美元。</p><p>乙花了20美元。</p>"
    report = report_from_html(1, "synthetic", "合成报告", EN, swapped)
    assert report.status == "fail"
    assert "mapping-facts" in {issue.code for issue in report.issues}
    assert report.display_notice

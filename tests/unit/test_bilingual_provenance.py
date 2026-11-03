"""持久回执、身份协议、源变化及旧稿保留；没有真实模型或数据库。"""

from copy import deepcopy

import pytest
from test_bilingual_regressions import ZA, ZA2, ZB, ZC, A, B, C, translate

from researchlib.render import render_body
from researchlib.translate import provenance as pr
from researchlib.translate.audit import classify
from researchlib.translate.display import article_pairs
from researchlib.translate.pair import stamp_paragraph_ids
from researchlib.translate.qa import report_from_html
from researchlib.translate.segments import iter_segments, splice_segments
from researchlib.translate.service import reuse_duplicate_sources


@pytest.mark.parametrize("other", [
    '<p>Run <code>x y</code> <a href="https://example.invalid/one">details</a>.</p>',
    '<p>Run <code>x  y</code> <a href="https://example.invalid/two">details</a>.</p>',
])
def test_compatibility_reuse_keeps_code_whitespace_and_resource_ownership(other):
    source = '<p>Run <code>x  y</code> <a href="https://example.invalid/one">details</a>.</p>'
    assert reuse_duplicate_sources([source, other], ["已有译文", None]) == ["已有译文", None]


def test_compatibility_reuse_can_only_fill_an_exact_empty_source_occurrence():
    assert reuse_duplicate_sources([A, A, A], [ZA, None, ZA2]) == [ZA, ZA, ZA2]


@pytest.mark.parametrize("mutation", [
    lambda rows: list(reversed(rows)),
    lambda rows: [{**rows[0], "id": "unknown"}, *rows[1:]],
    lambda rows: [rows[0], rows[0], rows[2]],
    lambda rows: rows[:-1],
    lambda rows: [{**rows[0], "source_hash": "wrong"}, *rows[1:]],
    lambda rows: [item["html"] for item in rows],
])
def test_unkeyed_swapped_missing_repeated_and_wrong_ids_are_rejected(tmp_path, mutation):
    with pytest.raises(pr.AlignmentError, match="response-"):
        translate(A + B + C, [ZA, ZB, ZC], tmp_path, mutate=mutation)


def test_wrong_text_with_correct_ids_is_rejected_when_numeric_anchors_disagree(tmp_path):
    with pytest.raises(pr.AlignmentError, match="numbers"):
        translate(A + B + C, [ZA, ZC, ZB], tmp_path)


def test_receipt_preserves_order_duplicate_occurrences_and_alternate_wording(tmp_path):
    conn = translate(A + A + B, [ZA, ZA2, ZB], tmp_path)
    stored = conn.writes[0]
    proof = stored[7].obj
    assert len({item["id"] for item in proof["segments"]}) == 3
    assert [item["ordinal"] for item in proof["segments"]] == [0, 1, 2]
    assert [item["path"] for item in proof["segments"]] == [[0, 0], [1, 0], [2, 0]]
    assert proof["segments"][0]["source_hash"] == proof["segments"][1]["source_hash"]
    assert proof["segments"][0]["target"] != proof["segments"][1]["target"]
    assert pr.verify(A + A + B, stored[3], proof).status == "mapped"
    assert proof["semantic_status"] == "not-reviewed"


def test_cleaner_output_change_breaks_compatibility_without_changing_original_hash():
    old_source = "<p>Archive header.</p>" + A
    old_target = "<p>归档头。</p>" + ZA
    proof = pr.receipt(pr.source_manifest(old_source), old_target)
    assert pr.verify(A, old_target, proof).reason == "source-fingerprint"
    assert classify(A, old_target, proof)["action"] == "review-or-retranslate-after-approval"


def test_pipeline_version_is_checked_even_if_text_is_unchanged(monkeypatch):
    proof = pr.receipt(pr.source_manifest(A), ZA)
    monkeypatch.setattr(pr, "PIPELINE", {**pr.PIPELINE, "segmenter": 999})
    assert pr.verify(A, ZA, proof).reason == "pipeline-version"


def test_receipt_tampering_or_target_drift_cannot_be_read_as_mapped():
    proof = pr.receipt(pr.source_manifest(A + B), ZA + ZB)
    corrupt = deepcopy(proof)
    corrupt["segments"].reverse()
    assert pr.verify(A + B, ZA + ZB, corrupt).status == "incompatible"
    assert pr.verify(A + B, ZA + ZC, proof).status == "incompatible"


def test_lossless_reconstruction_is_only_a_read_only_candidate():
    proof = pr.receipt(pr.source_manifest(A + B), ZA + ZB)
    candidate = pr.reconstruction(A + B, proof)
    assert candidate == ZA + ZB
    plan = classify(A + B, ZA + ZC, proof)
    assert plan["category"] == "lossless-candidate"
    assert plan["writes"] is False and plan["modelCalls"] == 0
    assert plan["candidateHash"] == pr.digest(candidate)
    assert pr.reconstruction(A + C, proof) is None


@pytest.mark.parametrize("md", [
    "> A note about demand and subscriber retention.\n\nA note about demand and subscriber retention.",
    "- Search: Customer demand improves retention.\n\n**Search:** Customer demand improves retention.",
    "- A note about demand and subscriber retention ↩\n\n"
    "A note about demand and subscriber retention [↩](#synthetic-note)",
])
def test_nested_export_quotes_lists_and_footnotes_keep_all_source_occurrences(md):
    source = str(render_body(md, {}))
    manifest = pr.source_manifest(source)
    normalized = manifest["source_snapshot"]
    target = normalized.replace("A note about demand and subscriber retention", "关于需求和订户留存的注释")
    target = target.replace("Customer demand improves retention", "客户需求提高留存").replace("Search", "搜索")
    proof = pr.receipt(manifest, target)
    view = article_pairs(source, target, provenance=proof)
    assert view.correspondence == "mapped" and not view.notice
    assert len(proof["segments"]) == len(iter_segments(source)) == 2
    assert len(view.pairs) == 2


def test_large_br_container_is_finely_mapped_and_keeps_resources():
    source = "<blockquote><p>" + "<br>".join(
        f"Topic {i} explains subscriber retention and the regional platform market." for i in range(45)
    ) + "</p></blockquote>"
    manifest = pr.source_manifest(source)
    normalized = manifest["source_snapshot"]
    target = splice_segments(normalized, {i: f"<p>议题{i}解释订户留存和区域平台市场。</p>" for i in range(45)})
    proof = pr.receipt(manifest, target)
    view = article_pairs(stamp_paragraph_ids(source), stamp_paragraph_ids(target), provenance=proof,
                         canonical_source=source, canonical_target=target)
    assert not view.notice and view.correspondence == "mapped" and len(view.pairs) == 45
    report = report_from_html(1, "synthetic", "合成", source, target, provenance=proof)
    assert report.status == "pass" and report.giant == ()
    assert report.as_dict()["semantics"] == "not-reviewed"


def test_ordered_list_item_values_and_inline_protected_code_survive_roundtrip():
    source = '<ol start="4"><li value="8">First.</li><li>Second.</li></ol><p>See <code>x = 42</code>.</p>'
    manifest = pr.source_manifest(source)
    target = manifest["source_snapshot"].replace("First", "第一项").replace("Second", "第二项").replace("See", "见")
    proof = pr.receipt(manifest, target)
    view = article_pairs(source, target, provenance=proof)
    shown = "".join(p.en + (p.zh or "") for p in view.pairs)
    assert 'value="8"' in shown and 'start="9"' in shown and "<code>x = 42</code>" in shown
    assert view.correspondence == "mapped"


def test_exact_duplicate_is_shown_once_and_keeps_the_sentence():
    view = article_pairs(A + A, ZA + ZA)
    assert view.notice == ""
    assert len(view.pairs) == 1
    assert "".join(p.en for p in view.pairs).count("Acme earned") == 1
    assert "10美元" in (view.pairs[0].zh or "")


@pytest.mark.parametrize("bad", [[], "not-an-object", 1, {"schema": 999}])
def test_invalid_persisted_receipt_is_classified_without_crashing(bad):
    assert pr.verify(A, ZA, bad).status == "incompatible"
    assert article_pairs(A, ZA, provenance=bad).notice
    assert classify(A, ZA, bad)["category"] == "unmapped"


def test_shared_media_does_not_erase_an_extra_target_occurrence_or_its_alt():
    source = '<p><img src="/media/a.png" alt="first"></p>'
    target = source + source.replace('alt="first"', 'alt="second"')
    view = article_pairs(source, target)
    visible = "".join(p.en + (p.zh or "") for p in view.pairs)
    assert visible.count('<img') == 2 and 'alt="second"' in visible


def test_correct_ids_do_not_claim_semantic_review_of_unanchored_text():
    source, target = "<p>Demand increased.</p>", "<p>需求下降。</p>"
    proof = pr.receipt(pr.source_manifest(source), target)
    report = report_from_html(1, "synthetic", "合成", source, target, provenance=proof)
    assert report.correspondence == "mapped" and report.semantic_status == "not-reviewed"


def test_legacy_coarse_paragraph_is_expanded_only_with_unique_ordered_anchors():
    source = "<blockquote><p>" + " ".join(
        f"Topic {i} explains the independent market mechanism "
        "and why customer retention improves revenue across regions."
        for i in range(25)
    ) + "</p></blockquote>"
    target = "<blockquote><p>" + "".join(
        f"议题{i}说明独立市场机制以及客户留存如何提高各地区收入。" for i in range(25)
    ) + "</p></blockquote>"
    view = article_pairs(source, target)
    assert not view.notice and view.correspondence == "legacy-anchors" and len(view.pairs) == 25
    report = report_from_html(1, "synthetic", "合成", source, target)
    assert report.status == "warn" and report.giant == () and report.display_gap == 0
    assert report.fine_gap == 24
    # 同样句数但数字反序，或所有段复用一个数字，都不能用句数发明对应关系。
    reversed_target = "<p>" + "".join(f"议题{i}解释市场机制。" for i in reversed(range(25))) + "</p>"
    assert article_pairs(source, reversed_target).notice
    assert article_pairs(source.replace("Topic ", "Topic 1 "), target).notice


@pytest.mark.parametrize("module", ["researchlib.mdlinks", "researchlib.web.app", "researchlib.translate.audit"])
def test_public_entry_imports_in_a_fresh_process(module):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("output,reason", [
    ('<p>见<a id="L1">二</a><a id="L0">一</a>。⟦0⟧⟦1⟧</p>', "links"),
    ('<p>见<a id="L0">一</a><a id="L1">二</a>。⟦1⟧⟦0⟧</p>', "media"),
])
def test_restored_resources_cannot_be_swapped_within_the_same_source_slot(tmp_path, output, reason):
    source = ('<p>See <a href="https://example.invalid/one">one</a>'
              '<a href="https://example.invalid/two">two</a>.'
              '<img src="/media/a.png"><img src="/media/b.png"></p>')
    with pytest.raises(pr.AlignmentError, match=reason):
        translate(source, [output], tmp_path)


def test_nested_footnote_ids_are_unique_and_all_fragment_links_resolve():
    from html.parser import HTMLParser

    source = ('<p>Demand<sup id="ref1"><a href="#note1">1</a></sup>.</p>'
              '<ol id="notes"><li id="note1">Footnote <a href="#ref1">back</a>.</li>'
              '<li id="note2">Next.</li></ol>')
    target = (source.replace("Demand", "需求").replace("Footnote", "注释")
              .replace("back", "返回").replace("Next", "下一项"))
    proof = pr.receipt(pr.source_manifest(source), target)
    view = article_pairs(source, target, provenance=proof)
    assert not view.notice

    class Facts(HTMLParser):
        ids = []
        hrefs = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.ids.append(attrs["id"])
            if attrs.get("href", "").startswith("#"):
                self.hrefs.append(attrs["href"][1:])

    parsed = Facts()
    parsed.feed("".join(p.en + (p.zh or "") for p in view.pairs))
    assert len(parsed.ids) == len(set(parsed.ids))
    assert set(parsed.hrefs) <= set(parsed.ids)

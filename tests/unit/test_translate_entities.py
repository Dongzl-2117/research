"""审核 T1：译文里的 AT&T 不再变成「AT&T;」，原文写着的 &lt;…&gt; 不再被当成标签吞掉。合成样本，不调模型。"""

from __future__ import annotations

from researchlib.htmlentities import entity_ref, escape_stray_ampersands, has_broken_entity, repair_entities
from researchlib.translate import provenance as pr
from researchlib.translate.display import article_pairs
from researchlib.translate.segments import iter_segments, shield, splice_segments, unknown_elements, unshield

AMP = "<p>AT&amp;T spends more on R&amp;D than the S&amp;P 500 average, per the Q&amp;A.</p>"
LT = "<p>When price&lt;cost holds and margin&gt;zero, profit rises.</p>"
TAG = "<p>He typed &lt;sarcasm&gt; after the joke.</p>"


def _written(source: str, answer: str) -> str:
    """模型交回 answer 以后写回、再切出来的那一段（和入库时同一条路）。"""
    value = unshield(answer, shield(source))
    assert value is not None
    whole = splice_segments(source, {0: value})
    return iter_segments(whole)[0].html


def test_ampersands_survive_both_ways_the_model_writes_them():
    for answer in ("AT&amp;T 的研发（R&amp;D）投入高于标普500（S&amp;P 500），见 Q&amp;A。",
                   "AT&T 的研发（R&D）投入高于标普500（S&P 500），见 Q&A。"):
        written = _written(AMP, answer)
        assert "&T;" not in written and "&D;" not in written and "&P;" not in written
        assert "AT&amp;T" in written and "S&amp;P 500" in written and "Q&amp;A" in written
        assert not has_broken_entity(written)


def test_less_than_signs_in_the_text_are_not_eaten_as_tags():
    for answer in ("当 price&lt;cost 成立且 margin&gt;zero 时，利润上升。",
                   "当 price<cost 成立且 margin>zero 时，利润上升。"):
        written = _written(LT, answer)
        assert "price&lt;cost" in written and "zero" in written and "成立" in written
        assert unknown_elements(written) == []
    written = _written(TAG, "他在笑话后面打了 &lt;sarcasm&gt;。")
    assert "&lt;sarcasm&gt;" in written


def test_escaped_markup_from_the_model_is_still_restored():
    source = '<p>See <a href="https://example.com/a?b=1&amp;c=2">this</a> and <em>that</em> <img src="a.png"></p>'
    answer = "见&lt;a id=&quot;L0&quot;&gt;这个&lt;/a&gt;和&lt;em&gt;那个&lt;/em&gt; &#10214;0&#10215;"
    value = unshield(answer, shield(source))
    assert value == '见<a href="https://example.com/a?b=1&amp;c=2">这个</a>和<em>那个</em> <img src="a.png">'
    # 原文自己把 <em> 当文字写出来的，模型照抄的 &lt;em&gt; 留作文字。
    value = unshield("&lt;em&gt; 标签和<em>强调</em>", shield("<p>The &lt;em&gt; tag vs <em>em</em></p>"))
    assert value == "&lt;em&gt; 标签和<em>强调</em>"


def test_stored_targets_are_repaired_when_reused():
    """11-11 译完的那批存着「AT&T;」：复用时改回 &amp;T，重签以后页面、只看中文页、检索块都对。"""
    broken = "<p>AT&T;的研发（R&D;）投入高于标普500（S&P; 500），见 Q&A;。</p>"
    manifest = pr.source_manifest(AMP)
    proof = pr.receipt(manifest, broken)
    targets = pr.reusable_targets(proof)[pr.reuse_key(AMP)]
    assert targets == ["<p>AT&amp;T的研发（R&amp;D）投入高于标普500（S&amp;P 500），见 Q&amp;A。</p>"]
    # 原文里真写着「&T;」的，分号留着。
    assert repair_entities("A&T;", "<p>A&amp;T; b</p>") == "A&amp;T;"
    assert repair_entities("&amp; &nbsp; &copy;") == "&amp; &nbsp; &copy;"


def test_a_target_that_swallowed_text_as_a_tag_is_not_reused():
    swallowed = "<p>当 price<cost holds and margin>zero 时，利润上升。</p>"
    proof = pr.receipt(pr.source_manifest(LT), swallowed)
    assert pr.reusable_targets(proof) == {}


def test_pages_show_old_rows_without_the_extra_semicolon():
    """库里还没重签的旧行：对照页显示时也不再多一个分号。"""
    broken = "<p>AT&T;的研发投入高于标普500。</p>"
    proof = pr.receipt(pr.source_manifest(AMP), broken)
    view = article_pairs(AMP, broken, provenance=proof)
    assert view.correspondence == "mapped"
    assert "AT&amp;T的" in view.pairs[0].zh and "&T;" not in view.pairs[0].zh


def test_entity_helpers():
    assert entity_ref("amp") == "&amp;" and entity_ref("nbsp") == "&nbsp;" and entity_ref("T") == "&amp;T"
    assert escape_stray_ampersands("AT&T &amp; &#39; &T;") == "AT&amp;T &amp; &#39; &amp;T"

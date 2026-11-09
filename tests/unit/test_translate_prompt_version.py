"""换提示词不拆已经签好的回执：对照页照常逐段配对；旧提示词译的篇再译时整篇按新提示词译。

合成样本，不调真模型、不连库。
"""

from __future__ import annotations

from test_bilingual_regressions import ZA, ZB, ZC, A, B, C
from test_translate_number_review import run, zh_for
from test_translate_segment_isolation import _beta_never_aligns, _previous

from researchlib.translate import provenance as pr
from researchlib.translate.prompts import SYSTEM_BODY

OLD_PIPELINE = {"cleaner": pr.CLEANER_VERSION, "segmenter": pr.SEGMENTER_VERSION, "prompt": 2}


def _old_receipt(source: str, translated: str) -> dict:
    """11-09 以前签的回执：提示词版本写在 pipeline 里，没有 prompt_versions。"""
    proof = pr.receipt(pr.source_manifest(source, pipeline=OLD_PIPELINE), translated)
    return {key: value for key, value in proof.items() if key != "prompt_versions"}


def _answer(segs, _repair):
    return [zh_for(segment) for segment in segs]


def test_receipts_signed_with_the_old_prompt_still_map_page_by_page():
    old = _old_receipt(A + B + C, ZA + ZB + ZC)
    assert old["pipeline"] == OLD_PIPELINE and pr.prompt_versions(old) == [2]
    assert pr.verify(A + B + C, ZA + ZB + ZC, old).status == "mapped"
    # 没变的段照样认得出来（重译失败时退回用）。
    assert pr.reusable_targets(old)[pr.reuse_key(B)] == [ZB]
    # 清洗或切段的版本变了，照旧作废。
    other = {**old, "pipeline": {**OLD_PIPELINE, "segmenter": pr.SEGMENTER_VERSION + 1}}
    assert pr.verify(A + B + C, ZA + ZB + ZC, other).reason == "pipeline-version"
    assert pr.reusable_targets(other) == {}
    # 旧回执被改过译文，照样查得出来。
    assert pr.verify(A + B + C, ZA + ZB + ZB, old).status == "incompatible"


def test_new_receipts_record_the_prompt_outside_the_pipeline():
    proof = pr.receipt(pr.source_manifest(A + B), ZA + ZB)
    assert proof["pipeline"] == pr.PIPELINE and "prompt" not in proof["pipeline"]
    assert proof["prompt_versions"] == [pr.PROMPT_VERSION] == pr.prompt_versions(proof)
    assert pr.verify(A + B, ZA + ZB, proof).status == "mapped"
    assert pr.prompt_versions(None) == [] and pr.prompt_versions({}) == []
    assert pr.prompt_versions({"prompt_versions": ["x"]}) == []


def test_an_article_from_the_old_prompt_is_retranslated_whole(tmp_path):
    """上一份回执是旧提示词译的：原文没变也不直接复用，三段都按新提示词重译。"""
    conn, _first, _calls = run(A + B + C, _answer, tmp_path)
    conn = _previous(conn)
    conn.previous["provenance"] = _old_receipt(A + B + C, conn.previous["body_html"])
    calls: list = []
    conn, result, _calls = run(A + B + C, _answer, tmp_path, conn=conn, calls=calls)
    assert [segs for segs, _repair in calls] == [[A, B, C]]
    assert result.status == "translated" and result.reused == 0
    stored = conn.writes[-1][7].obj
    assert stored["prompt_versions"] == [pr.PROMPT_VERSION] and stored["pipeline"] == pr.PIPELINE
    assert pr.verify(A + B + C, conn.writes[-1][3], stored).status == "mapped"


def test_a_failed_segment_falls_back_to_the_old_prompt_and_says_so(tmp_path):
    """整篇重译时乙段一直对不上：退回旧译文，不变成英文；回执记下这篇里混着两版提示词。"""
    conn, _first, _calls = run(A + B + C, _answer, tmp_path)
    conn = _previous(conn)
    conn.previous["provenance"] = _old_receipt(A + B + C, conn.previous["body_html"])
    conn, result, _calls = run(A + B + C, _answer, tmp_path, conn=conn, rows_hook=_beta_never_aligns)
    assert result.status == "translated" and result.reused == 1 and result.untranslated == 0
    stored = conn.writes[-1][7].obj
    assert stored["prompt_versions"] == [2, pr.PROMPT_VERSION]
    # 混着旧段的那篇，下次再译还是整篇按新提示词来。
    conn = _previous(conn)
    calls: list = []
    conn, _again, _calls = run(A + B + C, _answer, tmp_path, conn=conn, calls=calls)
    assert [segs for segs, _repair in calls][0] == [A, B, C]


def test_an_article_from_the_current_prompt_is_reused_without_asking(tmp_path):
    conn, _first, _calls = run(A + B + C, _answer, tmp_path)
    calls: list = []
    conn, again, _calls = run(A + B + C, _answer, tmp_path, conn=_previous(conn), calls=calls)
    assert calls == [] and again.reused == 3 and again.reason == "unchanged"


def test_the_prompt_keeps_the_number_rules_and_asks_for_plain_chinese():
    # 数字、标签、占位符这几条和 numbers.py、provenance.py 的核对对应，换写法时不能丢。
    for rule in ("上一季度、下一季度不是第1季度", "不要把 1.85 billion 写成 1.85亿", "$0.80 for every $1",
                 "<a id=\"L0\">文字</a>", "⟦0⟧", "segment_ids"):
        assert rule in SYSTEM_BODY
    for rule in ("不用破折号夹插入语", "长句拆短", "全文只用一种叫法", "全角「：」"):
        assert rule in SYSTEM_BODY

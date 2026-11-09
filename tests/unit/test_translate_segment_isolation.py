"""一段出错不连累整篇；已经译好、付过钱的段不丢；下次只补没译出来的段。

合成样本，不调真模型、不连库。模型替身按送来的段回译文（见 test_translate_number_review.run）。
"""

from __future__ import annotations

from unittest.mock import patch

from test_bilingual_regressions import ZA, ZB, ZC, A, B, C, MemoryConn
from test_translate_number_review import PLAIN, ZH_PLAIN, run, zh_for

from researchlib.translate import provenance as pr
from researchlib.translate import service
from researchlib.translate.display import article_pairs
from researchlib.translate.llm import LLMHTTPError, LLMTransportError
from researchlib.translate.qa import report_from_html
from researchlib.translate.segments import iter_segments


def _answer(segs, _repair):
    return [zh_for(segment) for segment in segs]


def _beta_never_aligns(rows):
    return [{**row, "source_hash": "wrong"} if "乙公司" in row["html"] else row for row in rows]


def _previous(conn):
    """把上一次写进假库的那份译文当成库里已有的译文。"""
    stored = conn.writes[-1]
    conn.previous = {"content_hash": stored[1], "title_zh": stored[2], "body_html": stored[3],
                     "complete": stored[5], "provenance": stored[7].obj}
    return conn


def test_a_partial_translation_is_finished_later_by_asking_only_the_missing_segment(tmp_path):
    conn, first, _calls = run(A + B + C, _answer, tmp_path, rows_hook=_beta_never_aligns)
    assert first.status == "partial" and conn.writes[0][7].obj["untranslated"] == [1]
    assert conn.writes[0][5] is False
    # 对照页照常按回执逐段配对；没译出来的那段只显示英文，不整页分开。
    view = article_pairs(A + B + C, conn.writes[0][3], provenance=conn.writes[0][7].obj)
    assert view.correspondence == "mapped" and not view.notice
    assert [pair.zh is None for pair in view.pairs] == [False, True, False]

    calls: list = []
    conn = _previous(conn)
    conn.previous["provenance"] = {**conn.previous["provenance"], "model": "earlier-model"}
    conn, second, _calls = run(A + B + C, _answer, tmp_path, conn=conn, calls=calls)
    stored = conn.writes[-1]
    assert second.status == "translated" and second.reused == 2 and second.untranslated == 0
    assert [segs for segs, _repair in calls] == [[B]]
    # 复用的两段是上一次的模型译的，回执里两个模型都在。
    assert stored[7].obj["model"] == "earlier-model, synthetic-model"
    assert stored[5] is True and stored[7].obj["untranslated"] == []
    assert stored[3].index("10美元") < stored[3].index("20美元") < stored[3].index("30美元")
    assert pr.verify(A + B + C, stored[3], stored[7].obj).status == "mapped"


def test_rerunning_a_finished_translation_costs_nothing_unless_fresh(tmp_path):
    conn, _first, _calls = run(A + B + C, _answer, tmp_path)
    calls: list = []
    conn, again, _calls = run(A + B + C, _answer, tmp_path, conn=_previous(conn), calls=calls)
    assert calls == [] and again.status == "translated" and again.reused == 3
    # 一字不差就不再写库，也不重切检索块。
    assert again.reason == "unchanged" and len(conn.writes) == 1

    calls = []
    conn, fresh, _calls = run(A + B + C, _answer, tmp_path, conn=conn, calls=calls, reuse=False)
    assert [len(segs) for segs, _repair in calls] == [3] and fresh.reused == 0


def test_fresh_retranslation_never_makes_a_good_translation_worse(tmp_path):
    """--fresh 整篇重译时哪段重译失败，那段退回已有的好译文，不变成英文。"""
    conn, _first, _calls = run(A + B + C, _answer, tmp_path)
    good = conn.writes[0][3]

    conn, result, _calls = run(A + B + C, _answer, tmp_path, conn=_previous(conn), reuse=False,
                               rows_hook=_beta_never_aligns)
    assert result.status == "translated" and result.reused == 1 and result.untranslated == 0
    assert result.reason == "unchanged" and len(conn.writes) == 1 and conn.writes[0][3] == good

    # 一段新的都没译成：不写库，报错交给 CLI，库里那份原样留着。
    try:
        run(A + B + C, lambda segs, _repair: [""] * len(segs), tmp_path, conn=conn, reuse=False)
    except pr.AlignmentError as exc:
        assert exc.code.endswith("-empty")
    else:
        raise AssertionError("整篇重译一段都没成时应该报错")
    assert len(conn.writes) == 1 and conn.attempts == []


def test_a_changed_paragraph_is_retranslated_and_the_rest_is_reused(tmp_path):
    conn, _first, _calls = run(A + B + C, _answer, tmp_path)
    edited = B.replace("20 dollars", "25 dollars")
    conn.article["body_md"] = A + edited + C
    conn.article["content_hash"] = "synthetic-after-edit"
    calls: list = []

    def answer(segs, _repair):
        return [ZB.replace("20美元", "25美元") if "25 dollars" in segment else zh_for(segment) for segment in segs]

    conn, result, _calls = run(A + edited + C, answer, tmp_path, conn=_previous(conn), calls=calls)
    assert [segs for segs, _repair in calls] == [[edited]]
    assert result.reused == 2 and "25美元" in conn.writes[-1][3]


def test_reuse_key_ignores_sentence_group_numbers_but_not_content():
    assert pr.reuse_key('<p data-src-para="3">Same words.</p>') == pr.reuse_key('<p data-src-para="7">Same words.</p>')
    assert pr.reuse_key("<p>Same words.</p>") != pr.reuse_key("<p>Other words.</p>")
    linked = '<p>See <a href="https://example.invalid/a">this</a>.</p>'
    assert pr.reuse_key(linked) != pr.reuse_key(linked.replace("/a", "/b"))


def test_reusable_targets_skip_english_gaps_tampered_targets_and_other_pipelines():
    manifest = pr.source_manifest(A + B + C)
    proof = pr.receipt(manifest, ZA + B + ZC, untranslated=[1])
    known = pr.reusable_targets(proof)
    assert pr.reuse_key(B) not in known
    assert known[pr.reuse_key(A)] == [ZA] and known[pr.reuse_key(C)] == [ZC]

    tampered = {**proof, "segments": [{**proof["segments"][0], "target": ZB}, *proof["segments"][1:]]}
    assert pr.reuse_key(A) not in pr.reusable_targets(tampered)
    assert pr.reusable_targets({**proof, "pipeline": {**proof["pipeline"], "segmenter": -1}}) == {}
    assert pr.reusable_targets({}) == {} and pr.reusable_targets(None) == {}


def test_a_dropped_connection_keeps_the_batches_already_paid_for(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "TRANSLATE_BATCH_MAX_SEGMENTS", 2)
    calls: list = []

    def answer(segs, _repair):
        if any("Gamma" in segment for segment in segs):
            raise LLMTransportError("连不上 DeepSeek 接口：ssl eof")
        return _answer(segs, _repair)

    conn, result, _calls = run(A + B + C, answer, tmp_path, calls=calls)
    stored = conn.writes[0]
    assert result.status == "partial" and result.reason == "request-failed"
    assert result.error == "连不上 DeepSeek 接口：ssl eof"
    # 断线是一时的：点名补译时命令行等几分钟自动再补没译出来的段。
    assert result.retry_later
    assert stored[7].obj["untranslated"] == [2] and "Gamma held 30 dollars" in stored[3]
    assert "10美元" in stored[3] and "20美元" in stored[3]
    # 断线后不再开新的一批，标题也不问了；冷却按请求失败算。
    assert [len(segs) for segs, _repair in calls] == [2, 1]
    assert stored[2] == "" and conn.attempts == [("failed", "request-failed")]


def test_running_out_of_balance_halfway_is_not_worth_retrying_soon(tmp_path, monkeypatch):
    """欠费隔几分钟再问也一样：已译的段照样存下，但不让命令行等着再补。"""
    monkeypatch.setattr(service, "TRANSLATE_BATCH_MAX_SEGMENTS", 2)

    def answer(segs, _repair):
        if any("Gamma" in segment for segment in segs):
            raise LLMHTTPError(402, "DeepSeek HTTP 402: Insufficient Balance")
        return _answer(segs, _repair)

    conn, result, _calls = run(A + B + C, answer, tmp_path)
    assert result.status == "partial" and result.reason == "request-failed"
    assert result.error == "DeepSeek HTTP 402: Insufficient Balance"
    assert not result.retry_later
    assert conn.attempts == [("failed", "request-failed")]


def test_a_dead_service_on_the_first_batch_still_fails_the_article(tmp_path):
    def answer(_segs, _repair):
        raise LLMHTTPError(402, "DeepSeek HTTP 402: Insufficient Balance")

    conn = MemoryConn(A + B)
    try:
        run(A + B, answer, tmp_path, conn=conn)
    except LLMHTTPError:
        pass
    else:
        raise AssertionError("一段都没译出来时要把错误交给 CLI 记冷却")
    assert conn.writes == [] and conn.attempts == []


def test_a_refused_segment_is_isolated_and_the_rest_is_written(tmp_path):
    def answer(segs, _repair):
        if any("Beta" in segment for segment in segs):
            raise LLMHTTPError(400, "DeepSeek HTTP 400: Content Exists Risk")
        return _answer(segs, _repair)

    conn, result, _calls = run(A + B + C, answer, tmp_path)
    assert result.status == "partial" and conn.writes[0][7].obj["untranslated"] == [1]
    # 为什么先放英文，命令输出和尝试记录里都有，不用翻日志。
    assert result.reason == "http-400"
    assert not result.retry_later
    assert conn.attempts == [("rejected", "untranslated:http-400")]


def test_a_translation_that_would_split_into_more_leaves_is_retried_as_one_paragraph(tmp_path):
    broken = "<p>平台主要分销渠道。<br>订户增长强劲。<br>区域优势稳定。<br>本季度利润为10美元。</p>"

    def answer(segs, repair):
        return [broken if "Acme" in segment and not repair else zh_for(segment) for segment in segs]

    conn, result, calls = run(A + B, answer, tmp_path)
    assert result.status == "translated"
    assert [len(segs) for segs, _repair in calls] == [2, 1]
    assert "拆成好几段" in calls[1][1]
    assert len(iter_segments(conn.writes[0][3])) == 2


def test_signing_drops_only_the_slot_that_changes_the_leaf_count():
    manifest = pr.source_manifest(A + B)
    html = manifest["source_snapshot"]
    split = "<p>第一行。<br>第二行。<br>第三行。<br>第四行。</p>"
    zh_html, proof, dropped = service._sign(manifest, html, {0: split, 1: ZB}, set(), model="synthetic-model")
    assert dropped == {0}
    assert proof["untranslated"] == [0]
    assert "Acme earned 10 dollars" in zh_html and "20美元" in zh_html
    assert pr.verify(A + B, zh_html, proof).status == "mapped"


def test_a_missing_title_is_the_only_thing_asked_next_time(tmp_path):
    conn, first, _calls = run(PLAIN, _answer, tmp_path, title="Distribution Notes", title_zh="")
    assert first.status == "partial" and conn.writes[0][2] == "" and conn.writes[0][7].obj["untranslated"] == []
    calls: list = []
    conn, second, _calls = run(PLAIN, _answer, tmp_path, title="Distribution Notes", title_zh="分发笔记",
                               conn=_previous(conn), calls=calls)
    assert calls == [] and second.status == "translated" and conn.writes[-1][2] == "分发笔记"
    assert ZH_PLAIN in conn.writes[-1][3]


def test_qa_lists_untranslated_slots_as_a_warning():
    en = A + B
    zh = ZA + B
    proof = pr.receipt(pr.source_manifest(en), zh, untranslated=[1])
    report = report_from_html(1, "synthetic", "合成", en, zh, provenance=proof)
    issues = {issue.code: issue for issue in report.issues}
    assert issues["untranslated"].level == "warn" and issues["untranslated"].count == 1
    # 先放英文的段数字当然和原文一样，不再算一次数字待核。
    assert "numbers-unverified" not in issues


def test_every_segment_failing_writes_nothing(tmp_path):
    with patch.object(service, "_SLOT_RETRIES", 1):
        try:
            run(A, lambda segs, _repair: [""], tmp_path)
        except pr.AlignmentError as exc:
            assert exc.code == "slot-0-empty"
        else:
            raise AssertionError("一段都没译出来时整篇不入库")


def test_partial_translation_keeps_receipt_untranslated_out_of_verify():
    en = A + B + C
    zh = ZA + B + ZC
    proof = pr.receipt(pr.source_manifest(en), zh, untranslated=[1])
    assert pr.verify(en, zh, proof).status == "mapped"
    assert pr.untranslated_slots(proof) == [1]
    assert pr.untranslated_slots({"untranslated": ["x"]}) == []

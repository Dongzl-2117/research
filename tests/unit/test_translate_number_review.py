"""数字核验：确认冲突的不入库；模型看不出、修一次仍不确定的写入并记 number_review。

串段、链接图片代码不对、身份一直对不上仍拒收。合成样本，不调真模型、不连库。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from test_bilingual_regressions import ZA, ZB, ZC, A, B, C, MemoryConn

from researchlib.translate import provenance as pr
from researchlib.translate import service
from researchlib.translate.display import article_pairs
from researchlib.translate.harness import _fold
from researchlib.translate.llm import ChatJsonResult, LLMTransportError
from researchlib.translate.numbers import _extract_located
from researchlib.translate.qa import report_from_html

# 规则认不出、但意思没错的两种写法：年代写成世纪，地点状语挪到前面。
DECADE = "<p>In the 1990s, Microsoft dominated the personal computer and set the terms for every developer.</p>"
STAFF = "<p>The company has 1,000 employees in 20 countries and plans to keep hiring next year.</p>"
PLAIN = "<p>Distribution still matters more than any single product feature in this market.</p>"
LINKED = '<p>Read <a href="https://example.invalid/memo">the memo</a> before the 2015 meeting starts.</p>'
ZH_DECADE = "<p>20世纪90年代，微软主导了个人电脑，为每一位开发者定下了规则。</p>"
ZH_STAFF = "<p>公司在20个国家有1000名员工，明年还打算继续招聘。</p>"
ZH_PLAIN = "<p>在这个市场上，分发仍然比任何单一产品功能更重要。</p>"
ZH_LINKED = '<p>在2015年会议开始前读一下<a id="L0">备忘录</a>。</p>'
ZH_LINK_DROPPED = "<p>在2015年会议开始前读一下备忘录。</p>"

_BY_KEYWORD = {
    "1990s": ZH_DECADE, "1,000": ZH_STAFF, "Distribution": ZH_PLAIN, "memo": ZH_LINKED,
    "Acme": ZA, "Beta": ZB, "Gamma": ZC,
}


def zh_for(segment: str) -> str:
    return next(text for key, text in _BY_KEYWORD.items() if key in segment)


def _facts(html: str):
    text, chosen = _extract_located(_fold(html))
    return text, [(fact, start, end) for fact, start, end in chosen if fact.hard]


def verifier_double(user: dict) -> dict:
    """规则认得出就按原文事实各交一条引用；认不出又没明显冲突时交不确定。"""
    source, target = user["source"], user["target"]
    source_text, source_spans = _facts(source)
    target_text, target_spans = _facts(target)
    if pr.numbers_agree(source, target):
        claims = []
        used = set()
        for fact, start, end in source_spans:
            pair = next(
                (
                    index for index, (other, _start, _end) in enumerate(target_spans)
                    if index not in used and other.kind == fact.kind and other.key == fact.key
                ),
                None,
            )
            if pair is None:
                continue
            used.add(pair)
            _other, target_start, target_end = target_spans[pair]
            source_quote = source_text[start:end]
            target_quote = target_text[target_start:target_end]
            claims.append({
                "source": source_quote, "target": target_quote,
                "object_source": source_quote, "object_target": target_quote, "same": True,
            })
        if claims:
            return {"evidence_id": user["evidence_id"], "verdict": "equivalent", "claims": claims}
    strong = {"qty", "pct", "clock", "date", "fiscal", "ratio"}
    source_keys = sorted(fact.key for fact, _start, _end in source_spans if fact.kind in strong)
    target_keys = sorted(fact.key for fact, _start, _end in target_spans if fact.kind in strong)
    if source_keys and target_keys and source_keys != target_keys:
        source_quote = source_text[source_spans[0][1]:source_spans[0][2]]
        target_quote = target_text[target_spans[0][1]:target_spans[0][2]]
        return {
            "evidence_id": user["evidence_id"], "verdict": "conflict",
            "claims": [{
                "source": source_quote, "target": target_quote,
                "object_source": source_quote, "object_target": target_quote, "same": False,
            }],
        }
    return {"evidence_id": user["evidence_id"], "verdict": "uncertain", "claims": []}


def run(body, answer, tmp_path, *, title="Synthetic audit", title_zh="合成标题", rows_hook=None, calls=None):
    """answer(segments, repair) 按送来的段回译文；少回几条就是条数不对。rows_hook 可以改坏身份。"""
    conn = MemoryConn(body)
    conn.article["title"] = title
    calls = [] if calls is None else calls

    def fake(**kwargs):
        user = kwargs["user"]
        if user.get("verify"):
            return ChatJsonResult(data=verifier_double(user), model="synthetic-model", usage=None, latency_ms=0)
        if user.get("segment_ids") == ["title"]:
            rows = [{"id": "title", "source_hash": user["source_hashes"][0], "html": title_zh}]
            return ChatJsonResult(data={"t": rows}, model="synthetic-model", usage=None, latency_ms=0)
        calls.append((list(user["segments"]), user.get("repair")))
        texts = answer(user["segments"], user.get("repair"))
        if user.get("repair") and "segment_ids" not in user:
            # 占位/链接丢了走 settle_unshield 的单段定向修复，没有段身份字段。
            return ChatJsonResult(data={"t": [texts[0]]}, model="synthetic-model", usage=None, latency_ms=0)
        # 故意不 strict：answer 少回几条时，模型那边就是少回了几条。
        rows = [{"id": identity, "source_hash": source_hash, "html": text}
                for identity, source_hash, text in zip(user["segment_ids"], user["source_hashes"], texts,
                                                       strict=False)]
        if rows_hook:
            rows = rows_hook(rows)
        return ChatJsonResult(data={"t": rows}, model="synthetic-model", usage=None, latency_ms=0)

    with patch.object(service, "chat_json", side_effect=fake), patch.object(service, "replace_translation_chunks"):
        result = service.translate_article(conn, 990001, data_dir=tmp_path)
    return conn, result, calls


def test_sample_wordings_are_still_unverified_by_the_rules():
    # 这几句是本次的前提：规则判不等，但译文没错。规则以后认得了，这里改成别的句子。
    assert pr.leaf_problem(DECADE, ZH_DECADE) == "numbers"
    assert pr.leaf_problem(STAFF, ZH_STAFF) == "numbers"
    assert pr.leaf_problem(PLAIN, ZH_PLAIN) is None


def test_unverifiable_numbers_are_written_and_listed_for_review(tmp_path):
    conn, result, calls = run(DECADE + STAFF + PLAIN, lambda segs, _repair: [zh_for(s) for s in segs], tmp_path)
    stored = conn.writes[0]
    proof = stored[7].obj
    assert result.status == "translated"
    assert result.review == 2
    assert proof["number_review"] == [0, 1]
    assert stored[5] is True
    assert pr.verify(DECADE + STAFF + PLAIN, stored[3], proof).status == "mapped"
    # 原批一次；核验没结论的两段各自定向补译一次（互不影响）。
    assert [len(segs) for segs, _repair in calls] == [3, 1, 1]
    assert calls[1][1] and "1990" in calls[1][1] and calls[2][1] and "1000" in calls[2][1]


def test_repaired_slot_replaces_the_unverified_one(tmp_path):
    fixed = "<p>1990年代，微软主导了个人电脑，为每一位开发者定下了规则。</p>"
    assert pr.leaf_problem(DECADE, fixed) is None

    def answer(segs, repair):
        return [fixed if repair and "1990s" in segment else zh_for(segment) for segment in segs]

    conn, result, _calls = run(DECADE + PLAIN, answer, tmp_path)
    stored = conn.writes[0]
    assert "1990年代" in stored[3] and "20世纪" not in stored[3]
    assert stored[7].obj["number_review"] == []
    assert result.review == 0


def test_repair_that_breaks_a_link_keeps_the_first_translation(tmp_path):
    source = '<p>Read <a href="https://example.invalid/memo">the memo</a> from the 1990s.</p>'
    first = '<p>读一下20世纪90年代的<a id="L0">备忘录</a>。</p>'

    def answer(segs, repair):
        return ["<p>读一下1990年代的备忘录。</p>"] if repair else [first]

    conn, result, _calls = run(source, answer, tmp_path)
    stored = conn.writes[0]
    assert "example.invalid/memo" in stored[3]
    assert stored[7].obj["number_review"] == [0]
    assert result.review == 1


def test_repair_request_failure_does_not_fail_the_article(tmp_path):
    def answer(segs, repair):
        if repair:
            raise LLMTransportError("连不上 DeepSeek 接口：ssl eof")
        return [zh_for(segment) for segment in segs]

    conn, result, _calls = run(DECADE + PLAIN, answer, tmp_path)
    assert result.status == "translated"
    assert conn.writes[0][7].obj["number_review"] == [0]


def test_numbers_moved_to_another_slot_still_reject(tmp_path):
    with pytest.raises(pr.AlignmentError, match="numbers"):
        run(A + B + C, lambda segs, _repair: [ZA, ZC, ZB][: len(segs)], tmp_path)


def test_misplaced_slot_needs_another_source_with_hard_numbers():
    sources = ["<p>Revenue rose 10% this year.</p>", "<p>No figures appear in this sentence.</p>"]
    dropped = ["<p>今年收入增长了。</p>", "<p>这句话里没有数字。</p>"]
    # 第 0 段漏了 10%，但第 1 段原文没有数字，不能拿「都没有数字」当串段。
    assert pr.misplaced_slot(sources, dropped, [0]) is None
    swapped_sources = ["<p>Revenue rose 10% this year.</p>", "<p>Profit rose 20% this year.</p>"]
    swapped = ["<p>今年收入增长20%。</p>", "<p>今年利润增长10%。</p>"]
    assert pr.misplaced_slot(swapped_sources, swapped, [0, 1]) == 0


def test_own_numbers_in_another_order_are_not_misplaced_even_if_a_neighbor_matches():
    # 下一段原文正好是「20 ... 1,000」的先后，但第 0 段的译文用的还是自己的那两个数。
    sources = [STAFF, "<p>Retail now spans 20 regions and 1,000 outlets across the continent.</p>"]
    targets = [ZH_STAFF, "<p>零售覆盖20个地区和1000家门店。</p>"]
    assert pr.numbers_agree(sources[1], ZH_STAFF)
    assert pr.misplaced_slot(sources, targets, [0]) is None


def test_count_mismatch_with_segment_ids_repairs_then_splits(tmp_path):
    """#217 之后少回一段直接整篇失败；现在照文档先重来一次，再对半拆开。"""

    def answer(segs, _repair):
        out = [zh_for(segment) for segment in segs]
        return out[:-1] if len(segs) > 1 else out

    conn, result, calls = run(A + B + C, answer, tmp_path)
    assert result.status == "translated"
    assert [len(segs) for segs, _repair in calls] == [3, 3, 1, 2, 2, 1, 1]
    assert calls[1][1] and "response-count" in calls[1][1]
    body = conn.writes[0][3]
    assert body.index("10美元") < body.index("20美元") < body.index("30美元")


def test_identity_errors_that_never_clear_stop_at_the_first_single_segment(tmp_path):
    """身份一直回错：原批、重来，拆到第一段单独一批再试两次就停，不再整批重走一遍。"""
    calls: list = []
    with pytest.raises(pr.AlignmentError, match="response-source-hash"):
        run(A + B + C, lambda segs, _repair: [zh_for(s) for s in segs], tmp_path, calls=calls,
            rows_hook=lambda rows: [{**rows[0], "source_hash": "wrong"}, *rows[1:]])
    assert [len(segs) for segs, _repair in calls] == [3, 3, 1, 1]


def test_dropped_link_is_retried_once_then_written(tmp_path):
    attempts: list[int] = []

    def answer(segs, _repair):
        attempts.append(len(segs))
        return [ZH_LINK_DROPPED] if len(attempts) == 1 else [ZH_LINKED]

    conn, result, _calls = run(LINKED, answer, tmp_path)
    assert result.status == "translated"
    assert "example.invalid/memo" in conn.writes[0][3]
    assert attempts == [1, 1]


def test_dropped_link_twice_still_rejects(tmp_path):
    with pytest.raises(pr.AlignmentError):
        run(LINKED, lambda segs, _repair: [ZH_LINK_DROPPED], tmp_path)


def test_title_numbers_written_differently_do_not_fail_the_article(tmp_path):
    conn, result, _calls = run(
        PLAIN, lambda segs, _repair: [zh_for(s) for s in segs], tmp_path,
        title="The 1990s Playbook", title_zh="20世纪90年代的打法",
    )
    assert result.status == "translated"
    assert conn.writes[0][2] == "20世纪90年代的打法"


def test_mapped_translation_keeps_pairs_when_a_sentence_moves_its_year():
    en = "<p>Apple grew revenue 10% in 2015.</p><p>There are 3 reasons.</p><p>No figures here.</p>"
    zh = "<p>苹果在2015年把收入提高了10%。</p><p>原因有3个。</p><p>这里没有数字。</p>"
    proof = pr.receipt(pr.source_manifest(en), zh)
    assert proof["number_review"] == []
    view = article_pairs(en, zh, provenance=proof)
    assert view.notice == ""
    assert view.correspondence == "mapped"
    assert len(view.pairs) == 3
    # 没有回执的旧稿仍按数字位置把关，对不上就分开展示。
    assert article_pairs(en, zh).notice


def test_qa_lists_number_review_slots_as_a_warning():
    en = DECADE + PLAIN
    zh = ZH_DECADE + ZH_PLAIN
    proof = pr.receipt(pr.source_manifest(en), zh)
    report = report_from_html(1, "synthetic", "合成", en, zh, provenance=proof)
    issues = {issue.code: issue for issue in report.issues}
    assert issues["numbers-unverified"].level == "warn"
    assert issues["numbers-unverified"].count == 1
    assert report.status == "warn"


def test_old_receipts_without_number_review_still_verify():
    proof = pr.receipt(pr.source_manifest(A + B), ZA + ZB)
    proof.pop("number_review")
    assert pr.verify(A + B, ZA + ZB, proof).status == "mapped"
    assert pr.unverified_numbers(proof) == []

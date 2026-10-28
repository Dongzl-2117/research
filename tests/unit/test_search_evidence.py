"""主体绑定边界用虚构公司验证，不用冻结验收文本充当产品规则。"""
from researchlib.mentions import CompanyMatcher
from researchlib.search_evidence import evidence_fingerprint, evidence_windows, local_evidence


def _matcher():
    return CompanyMatcher([
        {"id": 1, "name_zh": "轨道软件", "name_en": "Orbital", "aliases": []},
        {"id": 2, "name_zh": "星云软件", "name_en": "Nebula", "aliases": []},
    ])


def _row(body, **kwargs):
    return {"body": body, "title": "", "heading": "", **kwargs}


def test_named_query_is_not_an_assertion_but_negative_fact_is():
    matcher = _matcher()
    labels = local_evidence(_row("轨道软件 用户留存。"), matcher, {1}, ["用户", "留存"])
    assert not any(unit.confidence for unit in labels)
    facts = local_evidence(_row("轨道软件没有提高用户费用，固定价格降低了用户预算的不确定性。"),
                           matcher, {1}, ["用户", "留存"])
    assert facts[0].confidence == 1 and "没有提高" in facts[0].text


def test_foreign_subject_in_same_sentence_never_inherits_focus():
    units = local_evidence(
        _row("轨道软件与星云软件相比较，星云软件通过经销商签长期合同。", title="轨道软件"),
        _matcher(), {1}, ["合同"],
    )
    assert not any("经销商" in unit.text for unit in units)


def test_foreign_previous_subject_blocks_title_inheritance():
    row = _row("它的合同到期后会重新谈价格。", title="轨道软件", heading="轨道软件")
    units = local_evidence(row, _matcher(), {1}, ["合同"], previous=_row("星云软件刚调整了合同条款。"))
    assert units == []


def test_explicit_anaphor_can_cross_heading_without_losing_context():
    context = "这一节讨论轨道软件。"
    units = local_evidence(
        _row("它把服务费用分摊到每个账户。", heading="收费"), _matcher(), {1}, ["费用"],
        previous=_row(context, heading="对象"),
    )
    assert len(units) == 1 and units[0].context == context


def test_english_window_keeps_following_explanation_verbatim():
    text = "Orbital builds tools for teams. Its customers keep their project history when renewing."
    units = local_evidence(_row(text), _matcher(), {1}, ["retention"])
    window = evidence_windows(text, units)[0]
    assert window.text == text and text[window.start:window.end] == text


def test_duplicate_normalization_keeps_numbers_and_negation():
    assert evidence_fingerprint("合同金额 1.0 万。") != evidence_fingerprint("合同金额 10 万。")
    assert evidence_fingerprint("用户可以取消。") != evidence_fingerprint("用户不可以取消。")
    assert evidence_fingerprint("同一段\n  原文") == evidence_fingerprint("同一段 原文")

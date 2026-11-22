from researchlib.claims import claim_side, reversed_trend_title, source_quote
from researchlib.company_candidates import candidates
from researchlib.company_seed import SEEDS
from researchlib.mentions import CompanyMatcher, mentions_for_document
from researchlib.theses import validate_theses


def test_source_quote_preserves_financial_values_and_returns_source_spelling():
    for source, quote in [
        ("净利润同比增长-1.5%。", "净利润同比增长15%"),
        ("净利润同比变化-15%。", "净利润同比变化+15%"),
        ("上期毛利率为25%。", "上期毛利率为2.5%"),
        ("预计销售额为$100万。", "预计销售额为€100万"),
        ("模型规模为1 23亿。", "模型规模为123亿"),
        ("利润上升15.3%。", "利润上升15"),
        ("−15%的公司占多数。", "15%的公司占多数"),
    ]:
        assert source_quote(source, quote) == ""
    source = "原文：净利率为－１．５％，\n库存增加。"
    assert source_quote(source, "净利率为-1.5%，库存增加。") == source[3:]


def test_opposed_views_and_fabricated_evidence_do_not_change_into_consensus():
    yes = "公司毛利率将上升"
    no = "公司毛利率将下降"
    views = [
        dict(article_id=1, author="甲", judgment=yes, quote=yes + "，有望恢复增长。", stance="positive"),
        dict(article_id=2, author="乙", judgment=no, quote="原材料涨价使" + no + "。", stance="negative"),
    ]
    raw = [{"title": yes, "sources": [{"id": "v1", "side": "support"}, {"id": "v2", "side": "oppose"}],
            "evidence": ["v1：公司毛利率将上升99%，明年利润翻十倍。"]}]
    saved = validate_theses(raw, views)[0]
    assert saved["for"] == [1] and saved["against"] == [2]
    assert saved["evidence"] == ["v1：" + views[0]["quote"], "v2：" + views[1]["quote"]]
    assert "99%" not in str(saved["evidence"])
    assert claim_side("销量同比增长", "销量同比减少") == "oppose"
    assert claim_side("今年毛利率将上升", "明年毛利率将下降") == "unclear"
    assert claim_side("销量同比增长", "销量环比下降") == "unclear"
    assert claim_side("渠道库存下降", "渠道库存增长将拖累利润") == "oppose"


def test_company_symbol_aliases_and_unreviewed_candidates_remain_separate():
    companies = [dict(seed._asdict(), id=i) for i, seed in enumerate(SEEDS)]
    matcher = CompanyMatcher(companies)
    for symbol, name in (("700.HK", "腾讯"), ("01211.HK", "比亚迪"), ("9926.HK", "康方生物"),
                         ("Akeso Biopharma", "康方生物"), ("GOOG", "谷歌")):
        matches = mentions_for_document(matcher, symbol, "")
        assert [SEEDS[index].name_zh for index, *_ in matches] == [name]
    assert not mentions_for_document(matcher, "增长 9926 或 700 元", "")
    rows = [dict(id=1, title="合成企业 3456.HK", body_text="03456.HK 09926.HK", source_id="s1"),
            dict(id=2, title="合成企业公告", body_text="3456.HK", source_id="s2")]
    items = candidates(rows, companies, ["合成企业", "腾讯"])
    assert {item["candidate"] for item in items} == {"03456.HK", "合成企业"}
    assert all(item["articles"] == 2 and item["sources"] == ["s1", "s2"] for item in items)


def test_scope_distinguishes_customer_segment_and_forecast_period():
    assert claim_side("海外毛利率上升", "国内毛利率下降") == "unclear"
    assert claim_side("去年销量同比增长", "今年销量同比增长") == "unclear"


def test_opposite_financial_signs_survive_repost_deduplication():
    from researchlib.claims import same_repost

    a = {"judgment": "基于本轮业绩假设，净利润同比变化-15%，属于显著变化。", "quote": "净利润同比变化-15%。"}
    b = {key: value.replace("-15%", "+15%") for key, value in a.items()}
    assert not same_repost(a, b)


def test_literal_version_identifier_in_original_quote_is_not_replaced_with_a_reference():
    from researchlib.company_brief import evidence_lines

    line, = evidence_lines(["v1：本季度 v12 版本的销售额。"], {1: {"article_id": 1, "author": "甲"}})
    assert line["text"] == "本季度 v12 版本的销售额。"


def test_overlapping_opposite_theses_keep_each_authors_actual_side():
    up = "公司毛利率将上升"
    down = "公司毛利率将下降"
    views = [
        dict(article_id=1, author="甲", judgment="预计" + up, quote="产品升级带来溢价，" + up, stance="positive"),
        dict(article_id=2, author="乙", judgment=down, quote="单位采购成本抬高，公司毛利率将下降。", stance="negative"),
        dict(article_id=3, author="丙", judgment="明细显示" + down, quote="新渠道佣金较高，" + down, stance="negative"),
    ]
    candidates = [
        {"title": up, "for": [1], "against": [2]},
        {"title": down, "judgment": "供应商的议价能力上升，" + down, "for": [2, 3], "against": [1]},
    ]
    for order in (candidates, list(reversed(candidates))):
        accepted = validate_theses(order, views)
        assert accepted
        assert {ref for item in accepted for ref in item["sources"]} == {1, 2, 3}
        for item in accepted:
            assert claim_side(item["title"], item["judgment"]) == "support"
            for side, expected in (("for", "support"), ("against", "oppose")):
                assert all(claim_side(item["title"], views[ref - 1]["judgment"]) == expected for ref in item[side])


def test_contrasting_scope_and_conditional_risk_are_kept_in_the_thesis_detail():
    assert not reversed_trend_title("上半年毛利率上升", "下半年毛利率下降")
    assert not reversed_trend_title("毛利率上升", "如果材料涨价，毛利率下降")
    assert not reversed_trend_title("毛利率上升", "海外毛利率下降")

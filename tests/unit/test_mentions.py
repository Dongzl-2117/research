from researchlib.companies import validate_seed
from researchlib.company_seed import SEEDS
from researchlib.mentions import (
    DEPTH_PASSING,
    DEPTH_SECTION,
    DEPTH_TITLE,
    DEPTH_WHOLE,
    CompanyMatcher,
    classify_depth,
    mentions_for_document,
)


def matcher():
    companies = []
    names = {}
    for index, seed in enumerate(SEEDS, start=1):
        companies.append(
            {
                "id": index,
                "ticker": seed.ticker,
                "name_zh": seed.name_zh,
                "name_en": seed.name_en,
                "aliases": seed.aliases,
            }
        )
        names[seed.name_zh] = index
    return CompanyMatcher(companies), names


def test_seed_aliases_are_unique():
    validate_seed()
    assert len(SEEDS) >= 70


def test_longest_chinese_alias_and_company_separation():
    found, names = matcher()
    moutai = found.count("今天看贵州茅台，不是只说茅台两个字")
    assert moutai == {names["贵州茅台"]: 2}
    assert found.count("平安银行信贷") == {names["平安银行"]: 1}
    assert names["中国平安"] not in found.count("平安银行信贷")
    assert found.count("中国平安回购") == {names["中国平安"]: 1}


def test_latin_boundaries_and_case():
    found, names = matcher()
    assert found.count("metadata and meta analysis") == {}
    assert found.count("持有 META 和 Meta") == {names["Meta"]: 2}
    assert found.count("买入 aapl 与 Apple，不是 apple pie") == {names["苹果"]: 2}
    assert found.count("the cost of goods") == {}
    assert found.count("代码 COST") == {names["开市客"]: 1}
    assert found.count("Ping An Bank 不同于 Ping An")[names["平安银行"]] == 1
    assert found.count("Ping An Bank 不同于 Ping An")[names["中国平安"]] == 1


def test_depth_rules():
    assert classify_depth(1, [3], structured=True) == DEPTH_TITLE
    assert classify_depth(0, [1], structured=False) == DEPTH_PASSING
    assert classify_depth(0, [3], structured=False) == DEPTH_WHOLE
    assert classify_depth(0, [1, 0], structured=True) == DEPTH_PASSING
    assert classify_depth(0, [2, 0], structured=True) == DEPTH_SECTION
    assert classify_depth(0, [1, 1], structured=True) == DEPTH_WHOLE
    assert classify_depth(0, [6], structured=True) == DEPTH_WHOLE
    assert classify_depth(0, [0], structured=True) is None


def test_document_depth_uses_sections():
    found, names = matcher()
    body = """
# 季度流水账（示例）

## 汽车

比亚迪出货。比亚迪价格。

## 酒

茅台两个字带过。
"""
    found_rows = mentions_for_document(found, "季度流水账（示例）", body)
    rows = {company_id: depth for company_id, depth, _hits in found_rows}
    assert rows[names["比亚迪"]] == DEPTH_SECTION
    assert rows[names["贵州茅台"]] == DEPTH_PASSING


def test_analogy_and_one_liners_stay_passing():
    found, names = matcher()
    body = """
这篇先写宁德时代的产能。宁德时代的客户。宁德时代的价格。

比如腾讯当年、腾讯的做法、腾讯也这么干过，腾讯的例子到此为止。
"""
    rows = {company_id: depth for company_id, depth, _hits in mentions_for_document(found, "电池备忘", body)}
    assert rows[names["宁德时代"]] == DEPTH_WHOLE
    assert rows[names["腾讯"]] == DEPTH_PASSING

    long_aside = "\n\n".join(
        [
            "第一段在讲行业需求，没有点名。",
            "第二段继续讲价格。",
            "第三段只在一句里重复腾讯腾讯腾讯。",
            "第四段回到需求本身。",
        ]
    )
    aside = {company_id: depth for company_id, depth, _hits in mentions_for_document(found, "需求备忘", long_aside)}
    assert aside[names["腾讯"]] == DEPTH_PASSING

    short = "腾讯的广告、腾讯的视频号、腾讯的金融科技，都在这篇短帖里。"
    post = {company_id: depth for company_id, depth, _hits in mentions_for_document(found, "短帖", short)}
    assert post[names["腾讯"]] == DEPTH_WHOLE


def test_card_keeps_title_and_early_discussion_only():
    from researchlib.mentions import company_on_card

    body = "\n\n".join(
        [
            "开头两段都在讲宁德时代的产能和客户。宁德时代的价格也在这里。",
            "第二段还是宁德时代。",
            "后文才用比如腾讯来打比方。",
            "再往后用很长的篇幅把美团、美团、美团重复成全文。",
        ]
    )
    assert company_on_card("宁德时代的备忘", body, name="宁德时代", depth="title") is True
    assert company_on_card("行业备忘", body, name="宁德时代", depth="whole") is True
    assert company_on_card("行业备忘", body, name="腾讯", depth="whole") is False
    assert company_on_card("行业备忘", body, name="美团", depth="whole") is False
    assert (
        company_on_card("贵州茅台的渠道", "后文才写到别的事。", name="贵州茅台", aliases=("茅台",), depth="title")
        is True
    )


def test_matcher_is_fast_on_a_long_note():
    import time

    found, _names = matcher()
    text = "腾讯控股发布财报，同时提到美团、贵州茅台和 Apple。" * 4000
    started = time.perf_counter()
    counts = found.count(text)
    elapsed = time.perf_counter() - started
    assert counts
    assert elapsed < 1.5

from researchlib.citecheck import check_answer, split_claims

CITATIONS = [
    {
        "n": 1,
        "title": "腾讯的广告库存",
        "heading": "广告",
        "text": "腾讯的广告加载率还在提升，视频号是新的库存，广告收入同比增长 21%。",
        "author": "方伟",
        "date": "2025-03-12",
    },
    {
        "n": 2,
        "title": "茅台批价",
        "heading": "",
        "text": "飞天茅台的批价回到 2,300 元附近，渠道库存大约 1.5 个月。",
        "author": "乘风破浪",
        "date": "2025-06-02",
    },
]
AUTHORS = ["方伟", "乘风破浪", "孤独进化体"]


def test_split_claims_keeps_refs_after_the_full_stop():
    claims = split_claims("方伟认为视频号是新库存[1]。乘风破浪看批价。[2]\n- 第三点没有出处。")
    assert claims == [("方伟认为视频号是新库存[1]。", [1]), ("乘风破浪看批价。[2]", [2])]


def test_a_faithful_answer_has_no_flags():
    answer = (
        "方伟在 2025-03-12 认为视频号是腾讯新的广告库存，广告收入增长 21%[1]。\n"
        "乘风破浪说飞天茅台批价在 2300 元附近，渠道库存约 1.5 个月[2]。"
    )
    assert check_answer(answer, CITATIONS, AUTHORS) == []


def test_wrong_numbers_authors_dates_and_refs_are_flagged():
    answer = (
        "方伟认为广告收入增长 35%[1]。\n"
        "方伟说飞天茅台批价在 2300 元附近[2]。\n"
        "乘风破浪在 2024-01-05 提到渠道库存约 1.5 个月[2]。\n"
        "苹果的定价权很强[3]。\n"
        "作者说「视频号已经超过朋友圈」[1]。\n"
        "美团外卖的骑手成本和补贴节奏都在变化[1]。"
    )
    flags = check_answer(answer, CITATIONS, AUTHORS)
    problems = [" ".join(flag["problems"]) for flag in flags]
    assert any("35%" in item for item in problems)
    assert any("不是方伟写的" in item for item in problems)
    assert any("2024-01-05" in item for item in problems)
    assert any("不存在的出处 [3]" in item for item in problems)
    assert any("引号里" in item for item in problems)
    assert any("几乎没有一样的说法" in item for item in problems)
    assert all("[" not in flag["sentence"] for flag in flags)


def test_years_quarters_and_small_counts_are_not_checked():
    answer = "2025 年 Q3 方伟提到 3 个变化：视频号是新的库存[1]。"
    assert check_answer(answer, CITATIONS, AUTHORS) == []


def test_decimal_numbers_with_year_like_length_do_not_crash():
    """95.2 长度是 4，旧逻辑会当成年份去 int() 直接炸。"""
    answer = "方伟提到批价大约 95.2 元，渠道库存约 1.5 个月[2]。"
    flags = check_answer(answer, CITATIONS, AUTHORS)
    problems = " ".join(" ".join(flag["problems"]) for flag in flags)
    # 95.2 不在出处里，应被标出；关键是不要 500。
    assert "95.2" in problems

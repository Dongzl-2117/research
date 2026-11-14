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


ENGLISH = [
    {
        "n": 1,
        "title": "互补品的价格",
        "heading": "Aggregation",
        "text": (
            "Once again: demand for a product increases when the price of its complements decreases. "
            "The company sold forty-seven million units, and margins expanded to 31%."
        ),
        "author": "Ben Thompson",
        "date": "2024-05-06",
    },
]


def test_untranslated_english_passages_are_not_flagged_for_wording():
    """所引段落是没译的英文：中文转述和译成中文的引语字对字比不了，不算对不上。"""
    answer = (
        "互补品越便宜，需求越大[1]。\n"
        "作者的原话是「互补品价格下降时，产品需求上升」[1]。\n"
        "公司卖出 4700 万台，利润率升到 31%[1]。"
    )
    assert check_answer(answer, ENGLISH, ["Ben Thompson"]) == []


def test_english_passages_still_check_numbers_and_english_quotes():
    answer = (
        "公司卖出 5200 万台[1]。\n"
        "作者说「margins collapsed to 12%」[1]。\n"
        "作者说“demand for a product increases when the price of its complements decreases”[1]。"
    )
    flags = check_answer(answer, ENGLISH, ["Ben Thompson"])
    problems = [" ".join(flag["problems"]) for flag in flags]
    assert len(flags) == 2
    assert "数字 5200万 不在所引段落里" in problems[0]
    assert "数字 12% 不在所引段落里" in problems[1] and "引号里" in problems[1]


def test_spelled_out_english_numbers_match_digits():
    passage = [{**ENGLISH[0], "text": "They shipped forty-seven units, two hundred and five stores, a million users."}]
    answer = "卖了 47 台，开了 205 家店，有 1000000 个用户[1]。"
    assert check_answer(answer, passage, []) == []


def test_quotes_differing_only_in_particles_are_not_flagged():
    passage = [{**CITATIONS[0], "text": "开市客认为有限的选择会导致更高的销售额，这是它的新模式。"}]
    answer = "作者认为「有限选择会导致更高销售额」[1]。"
    assert check_answer(answer, passage, AUTHORS) == []
    rewritten = "作者认为「选择越少卖得越多」[1]。"
    flags = check_answer(rewritten, passage, AUTHORS)
    assert flags and "引号里" in " ".join(flags[0]["problems"])


def test_numbers_with_a_different_scale_are_compared_by_value():
    """英文写 $3.2 billion、47M，中文回答写 32 亿、4700 万：同一个数不算对不上，差了量级的照样标。"""
    passage = [{**ENGLISH[0], "text": "Revenue was $3.2 billion, up from $470M, with 1.5bn users and 250k stores."}]
    answer = "收入 32 亿美元，去年是 4.7 亿美元，用户 15 亿，门店 25 万家[1]。"
    assert check_answer(answer, passage, []) == []
    wrong = "收入 3.2 亿美元[1]。"
    flags = check_answer(wrong, passage, [])
    assert flags and "3.2亿" in flags[0]["problems"][0]
    # 中文段落里数和量级之间夹了字，照字面也算对上。
    chinese = [{**CITATIONS[0], "text": "视频号去年带来 4700多万 的广告收入，今年预计 3.2个亿。"}]
    assert check_answer("广告收入 4700 万，今年 3.2 亿[1]。", chinese, AUTHORS) == []


def test_dates_listed_without_repeating_the_year_are_checked_as_dates():
    """「2026-03-18/09-01」是同一年的两个日期：后一个补上年份按日期核对，月和日不当成数字。"""
    citations = [
        {"n": 1, "title": "开市客", "text": "它的模式是结构性的低成本。", "author": "方伟", "date": "2026-03-18"},
        {"n": 2, "title": "开市客", "text": "结构性的低成本，不靠差异化。", "author": "方伟", "date": "2026-09-01"},
    ]
    assert check_answer("方伟在 2026-03-18/09-01 都把它归为结构性的低成本[1][2]。", citations, AUTHORS) == []
    assert check_answer("方伟在 2026-03-18、09-01 都这么说[1][2]。", citations, AUTHORS) == []
    flags = check_answer("方伟在 2026-03-18/09-02 都把它归为结构性的低成本[1][2]。", citations, AUTHORS)
    assert flags[0]["problems"] == ["日期 2026-09-02 和所引段落对不上"]
    # 后面跟着量级、单位的是数的范围，照旧按数字查。
    flags = check_answer("方伟在 2026-03-18/10-20 亿元之间都这么说[1][2]。", citations, AUTHORS)
    assert "数字 10 不在所引段落里" in flags[0]["problems"]


def test_list_numbers_months_and_split_numbers_are_not_false_alarms():
    """分点序号、只写月份、两位年份不当数字核；原文里用换行隔开的两个数不粘在一起。"""
    body = "腾讯游戏收入增长很快，去年收入 100\n2024 年继续增长"
    cited = [{"n": 3, "author": "方伟", "date": "2024-12-05", "text": body}]
    assert check_answer("10. 方伟认为腾讯游戏收入增长很快[3]", cited, ["方伟"]) == []
    assert check_answer("（12）方伟认为腾讯游戏收入增长很快[3]", cited, ["方伟"]) == []
    assert check_answer("方伟在12月认为腾讯游戏收入增长很快[3]", cited, ["方伟"]) == []
    assert check_answer("方伟在24年认为腾讯游戏收入增长很快[3]", cited, ["方伟"]) == []
    assert check_answer("腾讯游戏去年收入为100[3]", cited, ["方伟"]) == []
    # 真的对不上照样报。
    flags = check_answer("腾讯游戏去年收入为120[3]", cited, ["方伟"])
    assert flags and "数字 120 不在所引段落里" in flags[0]["problems"]
    flags = check_answer("10. 腾讯游戏去年收入为1002024[3]", cited, ["方伟"])
    assert flags and "1002024" in flags[0]["problems"][0]

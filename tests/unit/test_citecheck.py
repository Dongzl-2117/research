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


def test_parenthesized_financial_cells_have_the_reported_sign():
    # OCR 把表格按竖线分列；末列没有外侧竖线。带括号的金额与正文里的注释、附注号区别处理。
    table = "摊销调整 | (5,867) | （.82） | ($782)\n其他 (3) | 167 | .02 | 0"
    cited = [{"n": 1, "text": table}]
    for correct, wrong in [("−5,867", "+5,867"), ("−0.82", "+0.82"), ("−782美元", "+782美元")]:
        assert not check_answer(f"摊销调整为{correct}[1]。", cited, [])
        assert check_answer(f"摊销调整为{wrong}[1]。", cited, [])
    notes = [{"n": 1, "text": "当期利润（12万元）列在附注 (17)。脚注(19) | 备注文字"}]
    assert not check_answer("利润为12万元，见附注17和19[1]。", notes, [])
    assert check_answer("利润为−12万元[1]。", notes, [])
    edge = [{"n": 1, "text": "(3.75) | (+2.25) | (-1.25)\n(17)"}]
    assert not check_answer("各项为−3.75、+2.25、−1.25，见附注17[1]。", edge, [])


def test_note_numbers_in_tables_are_not_accounting_losses():
    for note in ("附注", "Note", "脚注编号"):
        cited = [{"n": 1, "text": note + " | (17)"}]
        assert not check_answer("见附注17[1]。", cited, [])
        assert check_answer("调整金额为−17[1]。", cited, [])
    cited = [{"n": 1, "text": "| 项目 | Note | 2025年金额 |\n|---|---|---|\n| 摊销调整 | (17) | (782) |"}]
    assert not check_answer("见附注17，当期摊销为−782[1]。", cited, [])
    assert check_answer("见附注17，当期摊销为+782[1]。", cited, [])


def test_financial_evidence_preserves_precision_units_and_direction():
    cases = [
        ("同比增长12%", "同比增长12倍"), ("毛利率为12%", "毛利率为12.5%"),
        ("同比增长12%", "同比下降12%"), ("现金流为+12.5%", "现金流为−12.5%"),
        ("毛利率提高12个百分点", "毛利率提高12%"), ("收入32亿美元", "收入32亿人民币"),
    ]
    for claim, source in cases:
        assert check_answer(claim + "[1]。", [{"n": 1, "text": source}], []), (claim, source)
    assert not check_answer("同比下降12%[1]。", [{"n": 1, "text": "同比为−12%"}], [])


def test_uncited_statement_is_flagged_separately_from_headings_and_no_evidence():
    flags = check_answer("## 交易情况\n资料不足。\n公司已经退市。", [], [])
    assert [(flag["sentence"], flag["refs"]) for flag in flags] == [("公司已经退市。", [])]


def test_basis_points_compare_as_points_and_currency_does_not_cross():
    assert check_answer("毛利率提高0.25个百分点[1]。", [{"n": 1, "text": "毛利率提高25基点。"}], []) == []
    assert check_answer("成本增加12元[1]。", [{"n": 1, "text": "成本增加12美元。"}], [])


def test_signed_amounts_and_single_digit_foreign_currency_are_evidence():
    cited = [{"n": 1, "text": "Free cash flow was -$3, compared with HKD 7 in the previous release."}]
    assert not check_answer("自由现金流为−3美元[1]。", cited, [])
    assert not check_answer("上次披露的现金流为7港元[1]。", cited, [])
    for wrong in ("$3", "€7", "7日元", "£7"):
        assert check_answer(f"现金流为{wrong}[1]。", cited, [])


def test_punctuation_inside_a_cited_quote_does_not_detach_its_numbers():
    citation = [{"n": 7, "text": "上半年营收同比下降3.7%；净利润下降13.7%。"}]
    for opening, closing in [("「", "」"), ("“", "”"), ('"', '"')]:
        statement = f"他引用{opening}上半年营收同比下降3.7%；净利润下降13.7%。{closing}说明业绩走势。[7]"
        assert split_claims(statement) == [(statement, [7])]
        assert check_answer(statement, citation, []) == []
        flags = check_answer(statement.replace("13.7%", "13.7"), citation, [])
        assert len(flags) == 1 and flags[0]["refs"] == [7]
        assert flags[0]["problems"] == ["回答写作 13.7，出处中相同数值带百分号，请核对单位"]


def test_final_punctuation_inside_a_quote_keeps_the_following_sentence_separate():
    one = "原文写「先看收入。再看利润！」[1]"
    two = "另一篇持不同意见。[2]"
    assert split_claims(one + " " + two) == [(one, [1]), (two, [2])]
    broken = "原文写「没有闭引号。"
    assert split_claims(broken + two, include_uncited=True) == [(broken, []), (two, [2])]


SHARED = [
    {
        "n": 1,
        "title": "硬折扣",
        "heading": "",
        "before": "王小明：硬折扣的核心目标是高周转。",
        "text": (
            "商品毛利率限制在13-14%以内，未来能不能稳定赚 100 亿，轻乳茶大约3块钱左右每杯，"
            "2024 年净利润 34.0 亿元。"
        ),
        "author": "李四",
        "date": "2026-04-07",
    }
]


def test_clauses_before_a_trailing_ref_are_checked_against_it():
    # 出处写在整句末尾：分号前面的分句、行首加粗的小结都按这个出处核，不报「没有标注出处」。
    answer = "- **口径：毛利率压得很低。** 商品毛利率限制在 13%-14% 以内；一年能不能稳定赚 100 亿元是底线[1]。"
    assert check_answer(answer, SHARED, ["李四"]) == []
    wrong = "商品毛利率限制在 20% 以内；一年能不能稳定赚 100 亿元是底线[1]。"
    assert [flag["problems"] for flag in check_answer(wrong, SHARED, ["李四"])] == [["数字 20% 不在所引段落里"]]


def test_headings_and_notes_about_coverage_need_no_source():
    answer = (
        "**一、会员费才是利润来源**\n"
        "- 商品毛利率限制在 13%-14% 以内[1]。\n"
        "**覆盖说明**：资料只覆盖李四本人，时间从 2025-04 到 2026-10；没有其他作者的独立评论。\n"
        "这说明低价靠的是周转。"
    )
    flags = check_answer(answer, SHARED, ["李四"])
    assert [flag["sentence"] for flag in flags] == ["这说明低价靠的是周转。"]
    # 交代资料范围的话带着要核的数，照旧提示。
    assert check_answer("资料里它一年赚 350 亿元。", SHARED, ["李四"])[0]["problems"] == ["这句没有标注出处"]


def test_ranges_spoken_units_and_omitted_yuan_read_the_same_on_both_sides():
    answer = "毛利率在 13%～14% 之间，轻乳茶原料约 3 元一杯，稳定利润看 100亿元[1]。"
    assert check_answer(answer, SHARED, ["李四"]) == []
    # 出处写的是别的币种、或者小数目碰巧同值，不算对上。
    dollars = [dict(SHARED[0], text="未来能不能稳定赚 100 亿美元，库存 5 个月。")]
    assert check_answer("稳定利润看 100亿元[1]。", dollars, ["李四"])[0]["problems"] == ["数字 100亿元 不在所引段落里"]
    assert check_answer("一杯卖 5元[1]。", dollars, ["李四"])[0]["problems"] == ["数字 5元 不在所引段落里"]


def test_an_amount_after_a_year_is_not_read_as_a_date():
    # 以前「2024年34.0亿元」被读成日期 2024-34-00，34.0 亿这个数反而没核。
    assert check_answer("李四说 2025年净利润130.8亿元[1]。", SHARED, ["李四"])[0]["problems"] == [
        "数字 130.8亿元 不在所引段落里"
    ]
    assert check_answer("李四说 2024年34.0亿元[1]。", SHARED, ["李四"]) == []
    assert check_answer("李四在 2026.4.7 说过，2024年34.0亿元[1]。", SHARED, ["李四"]) == []


def test_words_from_the_question_and_names_in_context_are_not_flagged():
    # 「王小明：」在这段的前文里，不报「所引段落不是王小明写的」；问题里的说法加引号不算引原文。
    answer = "王小明认为硬折扣的“第一原则”是高周转[1]。"
    problems = check_answer(answer, SHARED, ["李四", "王小明"])[0]["problems"]
    assert problems == ["引号里的「第一原则」在所引段落里找不到"]
    assert check_answer(answer, SHARED, ["李四", "王小明"], question="硬折扣的第一原则是什么？") == []

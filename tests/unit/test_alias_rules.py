"""有歧义的别名：水果、地名、计量单位、「中美的」这类不能算成公司。"""

from researchlib.companies import validate_rules
from researchlib.company_seed import ALIAS_RULES, SEEDS
from researchlib.mentions import DEPTH_PASSING, CompanyMatcher, companies_fingerprint, mentions_for_document


def ruled():
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
                "alias_rules": ALIAS_RULES.get(seed.name_zh, {}),
            }
        )
        names[seed.name_zh] = index
    return CompanyMatcher(companies), names, companies


def depths(found, title, body):
    return {company_id: depth for company_id, depth, _hits in mentions_for_document(found, title, body)}


def test_rules_are_valid():
    validate_rules()


def test_fruit_apples_are_not_apple():
    found, names, _ = ruled()
    apple = names["苹果"]
    pdd = depths(
        found,
        "拼多多的农产品上行：从烟台苹果说起",
        "多多买菜把烟台苹果、阿克苏苹果卖到县城。\n\n拼多多的农产品心智来自这里。\n\n果农的苹果收购价在涨。",
    )
    assert apple not in pdd and names["拼多多"] in pdd
    nongfu = depths(
        found, "农夫山泉的17.5°苹果和橙子", "农夫山泉做 17.5°苹果，靠的是产区和分选。\n\n这门水果生意和饮料不同。"
    )
    assert apple not in nongfu and names["农夫山泉"] in nongfu


def test_apple_counts_when_the_article_is_about_the_company():
    found, names, _ = ruled()
    apple = names["苹果"]
    assert apple in depths(found, "手机笔记", "苹果在高端手机上的定价权很强，涨价也不掉量。")
    assert apple in depths(found, "苹果链的冷暖", "立讯精密给苹果链供货。苹果的订单在变。")
    rows = depths(found, "苹果收购", "苹果收购了一家做芯片的公司。库克说这是长期投入。苹果核心业务没变。")
    assert rows[apple] != DEPTH_PASSING
    # 讲 iPhone 的文章里顺带写到「烟台苹果」，那一处不算。
    doc = found.document("百亿补贴", "拼多多补贴 iPhone。烟台苹果也在卖。")
    hits = [doc_span for doc_span in doc.spans("拼多多补贴 iPhone。烟台苹果也在卖。") if doc_span[2] == apple]
    assert len(hits) == 1


def test_short_apple_posts_without_fruit_words_still_count():
    found, names, _ = ruled()
    apple = names["苹果"]
    # 没有佐证词，但也没有水果、农产品的字眼：照常算苹果公司。
    assert apple in depths(found, "短帖", "苹果的定价权很强，涨价也不掉量。苹果的回购还在继续。")
    # 讲水果的文章里出现了泛泛的「苹果」，没有佐证就不算。
    assert apple not in depths(found, "水果生意", "今年苹果的价格涨了。\n\n水果的损耗率很高。\n\n苹果卖得不错。")


def test_question_matching_has_no_needs_but_keeps_excludes():
    found, names, _ = ruled()
    apple = names["苹果"]
    # 问题里的「苹果」就是公司，不需要佐证。
    assert found.count("苹果的定价权") == {apple: 1}
    assert apple not in found.count("烟台苹果的价格")


def test_town_and_industry_phrases():
    found, names, _ = ruled()
    town = "国台、习酒都在茅台镇周边。\n\n茅台镇的产能扩张很快。\n\n茅台镇酱酒的渠道库存在涨。"
    assert names["贵州茅台"] not in depths(found, "酱酒复盘", town)
    assert found.count("贵州茅台在茅台镇") == {names["贵州茅台"]: 1}
    assert found.count("中国银行业的不良率") == {}
    assert found.count("中国银行的净息差") == {names["中国银行"]: 1}
    assert found.count("中国石油化工的炼化") == {names["中国石化"]: 1}
    assert found.count("中国移动互联网的红利") == {}
    assert found.count("中国移动的5G用户") == {names["中国移动"]: 1}
    assert found.count("伊利亚·苏茨克维离开了") == {}
    assert found.count("上海光大银行") == {}
    assert found.count("亚马逊雨林") == {}


def test_midea_bytedance_xiaomi_need_context():
    found, names, _ = ruled()
    midea = names["美的集团"]
    assert midea not in depths(found, "中美关系", "中美的关系在变。\n\n欧美的消费者更谨慎。\n\n完美的方案不存在。")
    rows = depths(found, "空调", "美的的空调份额在涨。美的和格力在价格上交手。美的的海外收入在涨。")
    assert midea in rows
    byte = names["字节跳动"]
    assert byte not in depths(found, "存储", "一个汉字占 3 字节。\n\n字节码要先编译。")
    assert byte in depths(found, "组织", "很多字节跳动的员工去了别处。字节的抖音还在涨。")
    xiaomi = names["小米"]
    assert xiaomi not in depths(found, "早餐", "小米粥和小米辣。\n\n小米是一种粮食。")
    assert xiaomi in depths(found, "汽车", "小米的 SU7 交付了。雷军说小米要做十年。")


def test_rules_change_the_fingerprint():
    _found, _names, companies = ruled()
    bare = [{**row, "alias_rules": {}} for row in companies]
    assert companies_fingerprint(companies) != companies_fingerprint(bare)

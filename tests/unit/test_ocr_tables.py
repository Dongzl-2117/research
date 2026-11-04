"""识图正文里的数据表识别：表头、数据行、表外的标题和注记。

样本取文章 24016 的两张图（OK 超市近 4 年、Costco FY2023/FY2024），
识图把表读成「一行一条记录，单元格之间一个空格」。
"""

from researchlib.ocr_tables import find_tables

OK = """OK超市近4年业绩汇总
项目 2020/3月末 2021/3月末 2022/3月末 2023/3月末
年销售额（亿元人民币） 217.6477 254.1157 262.1495 276.3207
销售增幅（%） 10.59% 16.77% 3.16% 5.49%
毛利率（%） 21.53% 21.86% 21.98% 21.89%
成本支出率（%） 16.11% 15.69% 16.04% 16.42%
净利润率（%） 5.42% 6.17% 5.94% 5.47%
年净利润（亿元人民币） 11.8256 15.7149 15.5991 15.1448
门店数（店） 123 128 134 142
店均年销售额（万元人民币） 17694.93 19852.79 19563.40 19459.20
店均日销售额（万元人民币） 48.48 54.39 53.60 53.31
卖场面积（㎡） 199250 208326 217420 230965
坪效（万元人民币） 10.92 12.20 12.06 11.96
员工人数（人）（兼职工按8小 13038 14873 15572 16284
人效（万元人民币） 166.93 170.86 168.35 169.69
注：根据OK财报汇总整理"""

COSTCO = """项目 FY2023 FY2024
年销售额（亿美元） 2377.10 2496.25
销售增幅 6.73% 5.01%
毛利率 10.57% 10.92%
费用率（SG&A） 9.08% 9.14%
会员费（亿美元） 45.80 48.28
净利润率 2.65% 2.95%
年净利润（亿美元） 62.92 73.67
门店数 861 890
店均年销售额（亿美元） 2.76 2.80
店均日销售额（万美元） 74.42 77.05
营业面积（万㎡） 1173.4 1216.1
坪效（万美元/㎡） 2.03 2.05
员工人数 ?16,000 333,000"""


def test_ok_super_table_header_and_rows(): 
    lines = OK.splitlines()
    tables = find_tables(lines)
    assert len(tables) == 1
    table = tables[0]
    # 标题在表外，表头从「项目」这一行开始，注记也留在表外。
    assert table.start == 1 and table.end == len(lines) - 1
    assert table.header == ("项目", "2020/3月末", "2021/3月末", "2022/3月末", "2023/3月末")
    assert table.width == 4
    assert len(table.rows) == 13
    first = table.rows[0]
    assert first.label == "年销售额（亿元人民币）"
    assert first.cells == ("217.6477", "254.1157", "262.1495", "276.3207")
    # 行尾是百分数的行也是数据行，不因为带 % 被当成正文。
    assert table.rows[1].cells == ("10.59%", "16.77%", "3.16%", "5.49%")


def test_costco_table_two_value_columns(): 
    table = find_tables(COSTCO.splitlines())[0]
    assert table.header == ("项目", "FY2023", "FY2024")
    assert table.width == 2
    assert len(table.rows) == 13
    # 识图在数字前留的问号不去掉，照原样进单元格。
    assert table.rows[-1].label == "员工人数"
    assert table.rows[-1].cells == ("?16,000", "333,000")


def test_wrapped_cell_text_joins_back_into_its_row(): 
    text = """项目 FY2023 FY2024
年销售额（亿美元） 2377.10 2496.25
店均年销售额（亿美
元） 2.76 2.80
门店数 861 890"""
    table = find_tables(text.splitlines())[0]
    assert len(table.rows) == 3
    assert table.rows[1].label == "店均年销售额（亿美元）"
    assert table.rows[1].cells == ("2.76", "2.80")


def test_prose_with_numbers_is_not_a_table(): 
    text = """我解释一下上面这句话：老巴收购 B 夫人的内布拉斯加家具城，给了她两个选择：现金，或是伯克希尔股票。
她选了现金，因为她只懂家具生意，但不懂股票。
我们买下 90% 股权，剩下 10% 留给参与经营的家族成员。
他每年赚 100 亿，连 3 年都不打折。"""
    assert find_tables(text.splitlines()) == []


def test_single_row_without_header_stays_prose(): 
    text = """年销售额说明
门店数 861 890"""
    assert find_tables(text.splitlines()) == []


def test_header_plus_one_row_is_a_table(): 
    text = """项目 2023 2024
营收 1.2 2.3"""
    table = find_tables(text.splitlines())[0]
    assert table.header == ("项目", "2023", "2024")
    assert table.rows[0].cells == ("1.2", "2.3")


def test_single_value_column_needs_a_header_and_three_rows(): 
    text = """项目 数值
门店数 861
毛利率 10.57%
员工人数 333000"""
    table = find_tables(text.splitlines())[0]
    assert table.header == ("项目", "数值")
    assert table.width == 1
    assert [row.label for row in table.rows] == ["门店数", "毛利率", "员工人数"]
    # 没有表头的一列数不当表，免得把正文里的「增长 3」排成表格。
    assert find_tables(["门店数 861", "毛利率 10.57%", "员工人数 333000"]) == []


def test_blank_line_ends_the_table(): 
    text = """项目 2023 2024
营收 1.2 2.3

门店数 861 890
库存 3 4"""
    tables = find_tables(text.splitlines())
    assert [(table.start, table.end) for table in tables] == [(0, 2), (3, 5)]


def test_first_row_is_never_treated_as_its_own_header(): 
    text = """营收 1.2 2.3
门店数 861 890"""
    table = find_tables(text.splitlines())[0]
    assert table.header is None
    assert table.start == 0 and table.end == 2

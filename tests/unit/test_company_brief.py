"""公司页概览和公司列表的归拢：按作者、按月数判断，画态度图，把论点文字里的内部编号换成读得懂的出处。"""

from researchlib import company_brief as cb


def _view(article_id, date, stance, author="方伟", **extra):
    item = {
        "article_id": article_id,
        "title": f"第 {article_id} 篇",
        "author": author,
        "date": date,
        "stance": stance,
        "judgment": f"判断 {article_id}",
    }
    item.update(extra)
    return item


def test_month_axis_covers_at_least_a_year_and_at_most_two():
    # 只有近三个月有判断：横轴照样铺满一年，图上看得出前面是空的。
    recent = [_view(1, "2026-09-03", "positive"), _view(2, "2026-11-01", "negative")]
    axis = cb.month_axis(recent, "2026-11")
    assert axis[0] == "2025-12" and axis[-1] == "2026-11" and len(axis) == 12
    # 最早的判断在五年前：只画最近两年。
    old = [_view(1, "2021-03-03", "positive"), _view(2, "2026-11-01", "negative")]
    axis = cb.month_axis(old, "2026-11")
    assert axis[0] == "2024-12" and axis[-1] == "2026-11" and len(axis) == 24
    # 没有日期的（飞书文库）不决定横轴；一条带日期的都没有也给一年。
    assert len(cb.month_axis([_view(1, "日期不详", "positive")], "2026-11")) == 12


def test_stance_chart_draws_up_for_positive_and_down_for_negative():
    axis = ["2026-10", "2026-11"]
    items = [
        _view(1, "2026-10-02", "positive"),
        _view(2, "2026-10-09", "positive"),
        _view(3, "2026-11-02", "negative"),
        _view(4, "2026-11-05", "mixed"),
        _view(5, "2026-11-06", "neutral"),
        _view(6, "日期不详", "negative"),
    ]
    series = cb.monthly(items, axis)
    # 没日期的不进图；两面和中性在图上是同一段。
    assert series == [
        {"ym": "2026-10", "positive": 2, "mixed": 0, "negative": 0, "neutral": 0},
        {"ym": "2026-11", "positive": 0, "mixed": 1, "negative": 1, "neutral": 1},
    ]
    svg = cb.stance_chart(series)
    assert svg.count('class="bar-pos"') == 1 and svg.count('class="bar-neg"') == 1
    assert svg.count('class="bar-mid"') == 1
    assert "2026-10：看好 2" in svg and "2026-11：两面 1，看淡 1，中性 1" in svg
    base = float(svg.split('class="axis" x1="0" y1="', 1)[1].split('"', 1)[0])
    positive_y = float(svg.split('class="bar-pos"', 1)[1].split('y="', 1)[1].split('"', 1)[0])
    negative_y = float(svg.split('class="bar-neg"', 1)[1].split('y="', 1)[1].split('"', 1)[0])
    assert positive_y < base < negative_y
    assert cb.stance_chart([]) == ""
    # 条数和图一一对应：两面加中性合成中间一个数。
    tally = cb.stance_tally(cb.stance_counts(items))
    assert [(part["label"], part["count"]) for part in tally] == [("看好", 2), ("两面或中性", 2), ("看淡", 2)]


def test_author_briefs_gives_cards_to_authors_who_said_enough():
    items = [
        _view(9, "2026-11-06", "mixed"),
        _view(8, "2026-10-20", "positive"),
        _view(7, "2026-10-15", "negative", author="李四"),
        _view(6, "2026-09-01", "positive"),
        _view(5, "2026-08-01", "positive"),
        _view(4, "日期不详", "positive", author="王小明"),
        _view(3, "日期不详", "mixed", author="王小明"),
        _view(2, "日期不详", "positive", author="王小明"),
    ]
    brief = cb.author_briefs(items, cb.month_axis(items, "2026-11"))
    # 图上这段时间说得多的在前。王小明三条都没日期：有卡片，但不画图。李四只有一条：占一行。
    assert [row["name"] for row in brief["main"]] == ["方伟", "王小明"]
    assert [row["name"] for row in brief["minor"]] == ["李四"]
    fangwei, undated = brief["main"]
    assert fangwei["chart"].startswith("<svg") and undated["chart"] == ""
    assert brief["charted"] is True
    assert [item["article_id"] for item in fangwei["latest"]] == [9, 8, 6]
    assert (fangwei["first"], fangwei["last"], fangwei["count"]) == ("2026-08-01", "2026-11-06", 4)
    assert (undated["first"], undated["last"]) == ("", "")
    assert brief["total"] == 8
    # 只有一位作者够一张卡时多列两条。
    solo = cb.author_briefs([item for item in items if item["author"] == "方伟"], ["2026-10", "2026-11"])
    assert len(solo["main"]) == 1 and len(solo["main"][0]["latest"]) == 4
    # 谁都不够三条：每位都给一张卡，都不画图，图例也不用出。
    two = [_view(1, "2026-11-01", "mixed"), _view(2, "2026-10-01", "mixed", author="李四")]
    few = cb.author_briefs(two, ["2026-11"])
    assert [row["name"] for row in few["main"]] == ["方伟", "李四"] and few["minor"] == []
    assert few["charted"] is False and all(row["chart"] == "" for row in few["main"])


def test_name_refs_turns_internal_ids_into_author_and_date():
    by_article = {
        460: _view(460, "2026-08-30", "negative", title="腾讯的战略 <AI>"),
        473: _view(473, "2026-09-16", "negative"),
        88: _view(88, "日期不详", "positive", author="王小明"),
    }
    # 前面没提作者：写作者加日期，连到那篇。标题进 title 属性时要转义。
    html = str(cb.name_refs("但v460认为投入是在赌，v473认为利润杯水车薪。", by_article))
    assert '<a class="ref" href="/articles/460" title="腾讯的战略 &lt;AI&gt;">方伟 2026-08-30</a>认为投入是在赌' in html
    assert ">方伟 2026-09-16</a>认为利润杯水车薪" in html
    # 前面紧挨着这位作者的名字，或者在括号里：只写日期，名字不说两遍。
    html = str(cb.name_refs("方伟在v460称回报存疑（v473、v460），王小明另有看法（v88）", by_article))
    assert "方伟在<a" in html and ">2026-08-30</a>称回报存疑" in html
    assert "（<a" in html and ">2026-09-16</a>、<a" in html
    assert '<a class="ref" href="/articles/88" title="第 88 篇">原文</a>' in html
    # 时间线上找不到的编号不给链接；别的字照样转义；像 v2 这样夹在英文里的不动。
    html = str(cb.name_refs("随后又说稳定（v999）<b>，iPhone v12x 和 Rev3 不算", by_article))
    assert "（另一篇）&lt;b&gt;" in html and "v12x" in html and "Rev3" in html
    # 原话开头的编号拆出来当出处。
    lines = cb.evidence_lines(["v473：AI 的利润相对开支是杯水车薪。", "没有编号的一句。", "v460："], by_article)
    assert [str(line["text"]) for line in lines] == ["AI 的利润相对开支是杯水车薪。", "没有编号的一句。"]
    assert ">方伟 2026-09-16</a>" in str(lines[0]["source"]) and lines[1]["source"] is None


def test_overview_shapes_theses_for_the_page():
    items = [_view(3, "2026-11-01", "positive"), _view(2, "2026-10-01", "negative"), _view(1, "日期不详", "mixed")]
    theses = [
        {
            "key": "t1",
            "title": "广告库存还能抬",
            "judgment": "广告库存还能抬",
            "mechanism": "加载率低",
            "evidence": ["v3：还能抬。"],
            "changes": "",
            "disagreement": "v2认为已经见顶",
            "articles": [3, 2, 1],
        }
    ]
    page = cb.overview(items, theses, 7, "2026-11")
    thesis = page["theses"][0]
    # 判断和标题一字不差的只写标题；最近一次说到它是哪天；链到只看这条论点的判断。
    assert thesis["judgment"] == "" and thesis["latest"] == "2026-11-01"
    assert thesis["href"] == "/companies/7?tab=views&thesis=t1"
    assert ">方伟 2026-10-01</a>认为已经见顶" in str(thesis["disagreement"])
    assert page["brief"]["main"][0]["href"] == "/companies/7?tab=views&author=%E6%96%B9%E4%BC%9F"


def test_judgments_filters_by_author_stance_and_thesis():
    items = [
        _view(6, "2026-11-06", "mixed"),
        _view(5, "2026-11-01", "negative", author="李四"),
        _view(4, "2026-10-20", "positive"),
        _view(3, "2026-10-02", "negative"),
        _view(2, "日期不详", "positive", author="李四"),
    ]
    theses = [{"key": "t1", "title": "论点一", "articles": [5, 3]}]
    everything = cb.judgments(items, theses, 7)
    assert [group["label"] for group in everything["groups"]] == ["2026 年 11 月", "2026 年 10 月", "日期不详"]
    assert [(row["label"], row["count"]) for row in everything["authors"]] == [
        ("全部作者", 5), ("方伟", 3), ("李四", 2),
    ]
    assert [(row["label"], row["count"]) for row in everything["stances"]] == [
        ("全部", 5), ("看好", 2), ("两面", 1), ("看淡", 2),
    ]
    assert everything["thesis"] is None and everything["total"] == 5
    # 每个筛选项上的数按另一项当前的选择算。
    negative = cb.judgments(items, theses, 7, stance="negative")
    assert [item["article_id"] for item in negative["items"]] == [5, 3]
    assert [(row["label"], row["count"]) for row in negative["authors"]] == [("全部作者", 2), ("方伟", 1), ("李四", 1)]
    assert negative["authors"][1]["href"] == "/companies/7?tab=views&author=%E6%96%B9%E4%BC%9F&stance=negative"
    one = cb.judgments(items, theses, 7, author="李四", stance="negative")
    assert [item["article_id"] for item in one["items"]] == [5]
    assert [(row["label"], row["count"], row["current"]) for row in one["stances"]] == [
        ("全部", 2, False), ("看好", 1, False), ("看淡", 1, True),
    ]
    # 只看一条论点；去掉论点时作者和态度的选择留着。认不得的态度、论点当作没选。
    scoped = cb.judgments(items, theses, 7, thesis_key="t1", author="方伟")
    assert [item["article_id"] for item in scoped["items"]] == [3]
    clear = "/companies/7?tab=views&author=%E6%96%B9%E4%BC%9F"
    assert scoped["thesis"] == {"key": "t1", "title": "论点一", "clear": clear}
    assert cb.judgments(items, theses, 7, stance="x", thesis_key="t9")["total"] == 5
    # 翻页：第一页不带 vpage，页码出界收到最后一页。
    paged = cb.judgments(items, theses, 7, page=9, size=2)
    assert (paged["page"], paged["pages"]) == (3, 3) and [item["article_id"] for item in paged["items"]] == [2]
    assert paged["prev"] == "/companies/7?tab=views&vpage=2" and paged["next"] == ""
    assert cb.judgments(items, theses, 7, page=2, size=2)["prev"] == "/companies/7?tab=views"


def test_index_marks_tabs_and_links():
    rows = [
        {"company_id": 1, "stance": "positive", "date": "2026-11-01"},
        {"company_id": 1, "stance": "neutral", "date": "2024-01-01"},
        {"company_id": 2, "stance": "negative", "date": "日期不详"},
    ]
    marks = cb.index_marks(rows, "2026-11")
    assert marks[1]["count"] == 2 and [part["count"] for part in marks[1]["tally"]] == [1, 1, 0]
    # 小图只画近一年：两年前那条和没日期的不进图，条数照算。
    assert marks[1]["chart"].count('class="bar-pos"') == 1 and 'class="bar-mid"' not in marks[1]["chart"]
    assert [part["count"] for part in marks[2]["tally"]] == [0, 0, 1] and 'class="bar-neg"' not in marks[2]["chart"]
    # 一条判断都没有的公司不分页。
    assert cb.tabs(7, 0, 12) == []
    assert [(tab["key"], tab["count"], tab["href"]) for tab in cb.tabs(7, 3, 12)] == [
        ("overview", None, "/companies/7"),
        ("views", 3, "/companies/7?tab=views"),
        ("articles", 12, "/companies/7?tab=articles"),
    ]
    assert cb.company_href(7, "overview", author="") == "/companies/7"


def test_decorate_views_links_to_the_quoted_passage():
    items = [
        _view(1, "2026-11-01", "positive", chunk=3, anchor="p-3", quote="原句", mechanism=""),
        _view(2, "2026-11-01", "positive", chunk=None, anchor="", quote="判断 2", mechanism=""),
    ]
    cb.decorate_views(items, lambda quote, judgment: quote == judgment)
    assert items[0]["href"] == "/articles/1?chunk=3#p-3" and items[0]["detail"] is True
    # 原句和判断是同一句、别的也没有：这一行不做成可展开的。
    assert items[1]["href"] == "/articles/2" and items[1]["quote_repeats"] is True and items[1]["detail"] is False

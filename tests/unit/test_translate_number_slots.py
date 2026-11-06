"""旧档案上 slot-*-numbers / unshield 的等值与拒绝。合成句子，不收真实文章。"""

from datetime import UTC, datetime, timedelta
from html import escape
from unittest.mock import patch

import pytest
from test_bilingual_regressions import MemoryConn, translate

from researchlib.translate import provenance as pr
from researchlib.translate.segments import shield, unshield
from researchlib.translate.service import (
    FAIL_RETRY_COOLDOWNS,
    REJECT_RETRY_BUDGET,
    REJECT_RETRY_COOLDOWN,
    retry_blocked,
)

P = "<p>{}</p>".format


def _problem(source: str, target: str) -> str | None:
    return pr.leaf_problem(P(source), P(target))


@pytest.mark.parametrize(("source", "target"), [
    ("Friday, March 29, 2013", "2013年3月29日，星期五"),
    ("The week of March 25-29, 2013", "2013年3月25日至29日这一周"),
    ("Notes from March 30-April 5, 2013 stay in the roundup.", "综述留着2013年3月30日至4月5日的记录。"),
    ("September 9, 2026 was the report date.", "报告日期是2026年9月9日。"),
    ("On Monday, March 25, 2013 the desk shipped four notes and 12 links.",
     "2013年3月25日星期一，小组发出了4篇笔记和12条链接。"),
    ("Revenue was $300,000 in the quarter.", "该季度收入为30万美元。"),
    ("China sales were $69.1 billion.", "中国销售额为691亿美元。"),
    ("The success rate was 100%.", "成功率是百分之百。"),
    ("The success rate was 100.0%.", "成功率是百分之百。"),
    ("FY2013 Q4 demand stayed firm.", "2013财年第4季度的需求仍然稳。"),
    ("Q3 revenue was 10 dollars.", "第3季度收入为10美元。"),
    ("In FY2014 the sample shipped 12 units.", "2014财年样本发出了12台。"),
    ("Voice was 400min ($40) on the sample plan.", "示例套餐的语音是400分钟（40美元）。"),
    ("The 24mo total was $2,160.", "24个月合计是2160美元。"),
    ("Profit was 10 dollars in March.", "3月利润为10美元。"),
    ("March and April sales were 4.", "4月和3月的销售额是4。"),
    ("Python3.10 is the runtime in this note.", "这篇笔记的运行时是3.10。"),
    ("H1 2013 revenue was discussed later.", "2013年上半年的收入放到后面再谈。"),
    ("1H 2013 revenue was booked then.", "随后计入2013年上半年的收入。"),
    ("H1 revenue of $10 million stayed in the note.", "上半年收入为1000万美元，仍写在这篇笔记里。"),
    ("In Q3, free cash flow was -$506 million vs. -$254 million in Q2’16.",
     "第三季度自由现金流为-5.06亿美元，而2016年第二季度为-2.54亿美元。"),
    ("In Q3, free cash flow was -$506 million vs. -$254 million in Q2’16.",
     "在第三季度，自由现金流为负5.06亿美元，而2016年第二季度为负2.54亿美元。"),
    ("one billion active devices in January 2016", "2016年1月有10亿台活跃设备"),
    ("There were three million books in print.", "在版图书有300万种。"),
    ("Media declined three percent.", "媒体下降了3%。"),
    ("Most phones are not a thousand dollars.", "大多数手机并不是1000美元。"),
    ("Hosting still costs $0.60.", "托管成本仍是60美分。"),
    ("Profit was 2 cents a share.", "利润为每股0.02美元。"),
    ("from $182 billion on April 23 to $207 billion on April 24.",
     "从4月23日的1820亿美元增至4月24日的2070亿美元。"),
    ("Revenue was up 116 percent from $112 million.", "收入较1.12亿美元增长了116%。"),
    ("Revenue was $243 million.", "收入为2亿4300万美元。"),
    ("Snap reported 166 million daily users.", "Snap报告了1亿6600万日活用户。"),
    ("The valuation was $438 million.1 Don’t feel too bad.", "估值是4.38亿美元。别太难受。"),
    ("Markets closed on April 24.2 It’s not a listing.", "市场在4月24日收盘。这不是上市。"),
    ("Q4’16 FCF stayed negative.", "2016年第四季度的自由现金流仍是负的。"),
    ("3Q 2013 results were late.", "2013年第3季度的业绩来晚了。"),
    ("3Q13 results were late.", "2013年第三季度的业绩来晚了。"),
    ("FY16 results were late.", "2016财年的业绩来晚了。"),
    ("Fourth-quarter revenue was $243 million.", "第四季度收入为2.43亿美元。"),
    ("Q3 revenue of $10 million came first.", "1000万美元的第三季度收入先到。"),
    ("Sales were $10-15 billion.", "销售额为100亿至150亿美元。"),
    ("Guidance was $80-82 billion.", "指引为800亿至820亿美元。"),
    ("Growth was 10-20%.", "增长为10%到20%。"),
    ("The print showed a (2)% decline.", "报表显示下降了2%。"),
    ("Cash flow was ($506 million).", "现金流为5.06亿美元。"),
    ("($2 billion is 2x Facebook and 4x Twitter.)", "（20亿美元是Facebook的2倍、Twitter的4倍。）"),
    ("There are 2 companies in the sample.", "样本里有两家公司。"),
    ("The round was $300k.", "这一轮是30万美元。"),
    ("The seed was $1.2m.", "种子轮是120万美元。"),
    ("The country has 1.4bn people.", "这个国家有14亿人。"),
    ("Ads were worth half a billion dollars.", "广告价值5亿美元。"),
    ("The service had 20 million users.", "这项服务有2千万用户。"),
    ("The cap hit $300 billion.", "市值达到3千亿美元。"),
    ("Profit was $50 million.", "利润为5千万美元。"),
    ("The roundup runs from March 30 to April 5, 2013.", "综述从2013年3月30日到4月5日。"),
    ("Notes cover March 25th-29th, 2013.", "笔记覆盖2013年3月25日至29日。"),
    ("The letter is dated the 29th of March, 2013.", "这封信的日期是2013年3月29日。"),
    ("The desk filed on 29 March 2013.", "小组在2013年3月29日交稿。"),
])
def test_archive_patterns_pass_the_slot_number_check(source, target):
    assert _problem(source, target) is None
    proof = pr.receipt(pr.source_manifest(P(source)), P(target))
    assert pr.verify(P(source), P(target), proof).status == "mapped"


@pytest.mark.parametrize(("source", "target"), [
    ("Profit was 10 dollars.", "利润为20美元。"),
    ("Revenue was $69.1 billion.", "收入为690亿美元。"),
    ("The success rate was 100%.", "成功率是百分之九十。"),
    ("September 9, 2026 was the report date.", "报告日期是2026年9月10日。"),
    ("FY2013 Q4 demand stayed firm.", "2013财年第3季度的需求仍然稳。"),
    ("Profit was 10 dollars then 20 dollars.", "利润先是20美元，然后是10美元。"),
    ("The desk shipped 12 links.", "小组发出了12条链接和2014年的附录。"),
    ("Version 3.10 is the runtime.", "运行时是3.1。"),
    ("Version 3.10.2 is the runtime.", "运行时是3.1.2。"),
    ("The company was founded in '48.", "公司于1948年成立。"),
    ("The price was $3.10.", "价格为3.2美元。"),
    ("Profit was 10 dollars in March.", "3月利润为20美元。"),
    ("March and April sales were 4.", "4月的销售额是3。"),
    ("Free cash flow was -$506 million.", "自由现金流为5.06亿美元。"),
    ("from $182 billion on April 23 to $207 billion on April 24.",
     "从4月24日的1820亿美元增至4月23日的2070亿美元。"),
    ("Margins went from 10% to 20%.", "利润率先是20%，然后是10%。"),
    ("Q3 and Q4 2013 were both discussed.", "2013年第4季度和第3季度都讨论了。"),
    ("Hosting costs $0.60.", "托管成本是50美分。"),
    ("Revenue was $243 million.", "收入为2亿美元。"),
    ("The valuation was $438 million.1 Don’t feel too bad.", "估值是4.38亿美元。2 别太难受。"),
    ("Devices were counted in January 2016 and December 2016.", "设备在2016年12月和2016年1月计数。"),
    ("Sales were $10-15 billion.", "销售额为10亿至15亿美元。"),
    ("Results arrived in Q2’16.", "业绩在2015年第二季度到来。"),
    ("The company reached one billion users.", "公司达到了1亿用户。"),
    ("Media declined three percent.", "媒体下降了4%。"),
    ("The company was founded in ’48.", "公司于1948年成立。"),
])
def test_changed_amounts_dates_and_versions_still_fail_the_slot(source, target):
    assert _problem(source, target) == "numbers"
    with pytest.raises(pr.AlignmentError, match="numbers"):
        pr.receipt(pr.source_manifest(P(source)), P(target))


def test_footnote_marker_can_change_wording_without_dropping_the_link_or_a_figure():
    source = '<p>Carriers differ here.<sup><a href="#fn-sample">1</a></sup> 12 links shipped.</p>'
    kept = '<p>这里的运营商不同。<sup><a href="#fn-sample">注</a></sup>发出了12条链接。</p>'
    assert pr.leaf_problem(source, kept) is None
    assert pr.leaf_problem(source, "<p>这里的运营商不同。<sup><a href=\"#fn-sample\">注</a></sup></p>") == "numbers"
    assert pr.leaf_problem(source, "<p>这里的运营商不同。发出了12条链接。</p>") == "links"


def test_may_be_is_not_a_month_and_may_the_month_is():
    assert _problem(
        "Bitcoin May Be the last haven in this note.",
        "比特币也许是这篇笔记里最后的避风港。",
    ) is None
    assert _problem(
        "Bitcoin May Be the last haven in this note.",
        "比特币在5月成为这篇笔记的避风港。",
    ) == "numbers"
    assert _problem("Sales in May were 10.", "5月销售额为10。") is None


def test_bookmark_anchor_is_not_an_unshield_failure():
    raw = '<p>See <a href="https://example.invalid/one" title="see > more">note</a><a name="week-mark"></a>.</p>'
    shielded = shield(raw)
    assert shielded.html.count('id="L') == 1
    assert "week-mark" not in shielded.html
    assert ">note</a>" in shielded.html
    restored = unshield(shielded.html.replace("note", "注释"), shielded)
    assert restored is not None
    assert "example.invalid/one" in restored
    assert pr.leaf_problem(raw, restored) is None


def test_fullwidth_image_token_still_unshields():
    raw = '<p>See <img src="/x.png" alt="">.</p>'
    shielded = shield(raw)
    restored = unshield("<p>见图【0】。</p>", shielded)
    assert restored is not None and 'src="/x.png"' in restored


def test_swapped_dollar_slots_still_reject(tmp_path):
    with pytest.raises(pr.AlignmentError, match="numbers"):
        translate(
            "<p>Acme earned 10 dollars from new subscribers while the platform kept its distribution.</p>"
            "<p>Beta lost 20 dollars after a different customer group reduced demand for infrastructure.</p>",
            [
                "<p>甲公司从新订户赚了20美元，仍保持平台分发。</p>",
                "<p>乙公司因另一类客户需求减少而亏损10美元。</p>",
            ],
            tmp_path,
        )


def test_archive_shaped_article_translates_without_a_slot_number_reject(tmp_path):
    title = "Week of March 25-29, 2013"
    body = """
<p>Friday, March 29, 2013</p>
<p>On Monday, March 25, 2013 the sample desk shipped four notes and 12 links.</p>
<p>Carriers differ here.<sup><a href="#fn-sample">1</a></sup><a name="week-mark"></a></p>
<table>
<tr><td>Voice</td><td>400min ($40)</td><td>Unlimited ($50)</td></tr>
<tr><td>Data</td><td>3GB ($30)</td><td>2.5GB ($10)</td></tr>
<tr><td>24mo total</td><td>$2,160</td><td>$1,440</td></tr>
</table>
<p>China sales were $69.1 billion and the success rate was 100%.</p>
<p>The roundup covers March 30-April 5, 2013 and FY2013 Q4 demand.</p>
""".strip()
    conn = MemoryConn(body)
    conn.article["title"] = title
    conn.article["raw_meta"] = {"format": "html", "language": "en"}

    def localize(segment: str) -> str:
        text = segment
        for src, dst in (
            ("Friday, March 29, 2013", "2013年3月29日，星期五"),
            ("On Monday, March 25, 2013 the sample desk shipped four notes and 12 links.",
             "2013年3月25日星期一，样本组发出了4篇笔记和12条链接。"),
            ("Carriers differ here.", "这里的运营商不同。"),
            ("Voice", "语音"),
            ("400min ($40)", "400分钟（40美元）"),
            ("Unlimited ($50)", "不限量（50美元）"),
            ("Data", "流量"),
            ("3GB ($30)", "3GB（30美元）"),
            ("2.5GB ($10)", "2.5GB（10美元）"),
            ("24mo total", "24个月合计"),
            ("$2,160", "2160美元"),
            ("$1,440", "1440美元"),
            ("China sales were $69.1 billion and the success rate was 100%.",
             "中国销售额为691亿美元，成功率是百分之百。"),
            ("The roundup covers March 30-April 5, 2013 and FY2013 Q4 demand.",
             "综述覆盖2013年3月30日至4月5日以及2013财年第4季度的需求。"),
            (title, "2013年3月25日至29日这一周"),
        ):
            text = text.replace(src, dst)
        return text

    def fake(**kwargs):
        from researchlib.translate.llm import ChatJsonResult

        user = kwargs["user"]
        segments = user["segments"]
        if segments == [escape(title)]:
            values = [localize(escape(title))]
        else:
            values = [localize(segment) for segment in segments]
        if "segment_ids" in user:
            values = [
                {"id": identity, "source_hash": source_hash, "html": html}
                for identity, source_hash, html in zip(
                    user["segment_ids"], user["source_hashes"], values, strict=True,
                )
            ]
        return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)

    from researchlib.translate import service

    with patch.object(service, "chat_json", side_effect=fake), patch.object(service, "replace_translation_chunks"):
        result = service.translate_article(conn, 990001, data_dir=tmp_path)
    assert result.status == "translated"
    stored = conn.writes[0][3]
    assert "691亿" in stored and "百分之百" in stored
    assert "2013年3月29日" in stored
    assert "#fn-sample" in stored
    assert "week-mark" not in stored


def test_sentence_split_ids_do_not_change_the_source_fingerprint():
    """长段按句切开时，同一篇原文译前译后各算一次指纹必须相同。

    以前编号挂在进程全局计数器上，第二次 materialize 会把 data-src-para 换成新号，
    译文已经对齐也会被记成 canonical-source-changed。
    """
    sentence = "The sample desk kept shipping notes while the market stayed open. "
    html = "<p>" + sentence * 40 + "Revenue was $10 million in the quarter.</p>"
    assert len(html) > 900
    first = pr.source_manifest(html)
    second = pr.source_manifest(html)
    assert first["source_fingerprint"] == second["source_fingerprint"]
    assert "data-src-para" in first["source_snapshot"]


def test_long_article_is_not_rejected_as_canonical_source_changed(tmp_path):
    sentence = "The sample desk kept shipping notes while the market stayed open. "
    body = "<p>" + sentence * 40 + "Revenue was $10 million in the quarter.</p>"
    conn = MemoryConn(body)
    conn.article["title"] = "Long note"
    conn.article["raw_meta"] = {"format": "html", "language": "en"}

    def fake(**kwargs):
        from researchlib.translate.llm import ChatJsonResult

        user = kwargs["user"]
        segments = user["segments"]
        if segments == [escape("Long note")]:
            values = ["长笔记"]
        else:
            values = []
            for segment in segments:
                if "$10 million" in segment:
                    values.append("<p>该季度收入为1000万美元。</p>")
                else:
                    values.append("<p>这是一句中文说明。</p>")
        if "segment_ids" in user:
            values = [
                {"id": identity, "source_hash": source_hash, "html": html}
                for identity, source_hash, html in zip(
                    user["segment_ids"], user["source_hashes"], values, strict=True,
                )
            ]
        return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)

    from researchlib.translate import service

    with patch.object(service, "chat_json", side_effect=fake), patch.object(service, "replace_translation_chunks"):
        result = service.translate_article(conn, 990001, data_dir=tmp_path)
    assert result.status == "translated"
    assert "1000万美元" in conn.writes[0][3]


def test_placeholder_spellings_and_link_ids_still_roundtrip():
    image = '<p>See <img src="/x.png" alt="">.</p>'
    shielded = shield(image)
    for wording in ("<p>见图[[0]]。</p>", "<p>见图【图0】。</p>", "<p>见图&#10214;0&#10215;。</p>"):
        restored = unshield(wording, shielded)
        assert restored is not None and 'src="/x.png"' in restored
    assert unshield("<p>见图。</p>", shielded) is None

    one = '<p>See <a href="https://example.invalid/one">example.invalid/one</a>.</p>'
    shielded = shield(one)
    by_href = unshield('<p>见 <a href="https://example.invalid/one">例子</a>。</p>', shielded)
    assert by_href is not None and 'href="https://example.invalid/one"' in by_href
    assert unshield('<p>见 <a href="https://example.invalid/two">例子</a>。</p>', shielded) is None
    shifted = unshield('<p>见 <a id="L1">例子</a>。</p>', shielded)
    assert shifted is not None and "example.invalid/one" in shifted

    both = (
        '<p><a href="https://example.invalid/one">one</a> and '
        '<a href="https://example.invalid/two">two</a>.</p>'
    )
    shielded = shield(both)
    restored = unshield('<p><a id="L1">甲</a>与<a id="L2">乙</a>。</p>', shielded)
    assert restored is not None
    assert restored.index("example.invalid/one") < restored.index("example.invalid/two")
    assert unshield('<p><a id="L0">甲</a>。</p>', shielded) is None


def test_retry_budget_and_cooldown_match_the_queue_rules():
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    assert retry_blocked([], now=now) is False
    recent = now - timedelta(minutes=1)
    assert retry_blocked([("rejected", recent)], now=now) is True
    cooled = now - REJECT_RETRY_COOLDOWN - timedelta(minutes=1)
    assert retry_blocked([("rejected", cooled)], now=now) is False
    assert retry_blocked([("rejected", cooled), ("rejected", cooled)], now=now) is True
    assert len([("rejected", cooled)] * REJECT_RETRY_BUDGET) == REJECT_RETRY_BUDGET
    assert retry_blocked([("failed", now - timedelta(minutes=1))], now=now) is True
    assert retry_blocked(
        [("failed", now - FAIL_RETRY_COOLDOWNS[0] - timedelta(minutes=1))],
        now=now,
    ) is False
    many = [("failed", now - timedelta(hours=1))] * len(FAIL_RETRY_COOLDOWNS)
    assert retry_blocked(many, now=now) is True
    rested = [("failed", now - FAIL_RETRY_COOLDOWNS[-1] - timedelta(minutes=1))] * 8
    assert retry_blocked(rested, now=now) is False

"""空回答不入库，问答页按问题 / 回答分开。"""

from researchlib.author_voice import duan_paragraphs
from researchlib.empty_answer import (
    hide_empty_answer,
    is_chrome_line,
    normalize_voice,
    stored_answer_empty,
    substantive_voice,
    voice_is_empty,
)
from researchlib.ingest.duanyongping.emit import render_markdown
from researchlib.ingest.duanyongping.extract import Block
from researchlib.ingest.duanyongping.reclassify import decide_stored
from researchlib.ingest.duanyongping.segment import Post, classify, segment_post
from researchlib.mdlinks import prepare_body_html
from researchlib.qa_layout import render_qa_html

COOK = (
    "## 读者问\n\n"
    "Tim Cook wrote to employees when Apple crossed one trillion dollars. "
    "He said the work is not done and the next decade still has to be built one product at a time. "
    "Products have to stay focused on the people who use them every day. "
    "苹果市值到了一万亿美元，这封内部信把过程从头讲了一遍，从第一家店写到现在的产品线。"
    "读者把全文贴上来，想问段永平怎么看这家公司的生意，信里还写了团队和零售店。"
    "原先这些话挤在同一段里，读起来没有停顿。\n\n"
    "## 段永平\n\n"
    "千 里 牛 2018-08-03 11:55 回 本 文 开 头 ↑\n"
)

PDD = """## 读者问

拼多多的增长很快，但利润和补贴到底怎么看，能不能拿几年。对照一下 Meta 的广告模式，读者写了很长一篇，标题被截成了省略号。

## 段永平

李历 2018-08-05 23:48 回本文开头 ↑
"""

REAL = """## 读者问

苹果现在贵不贵？生意模式还在吗？

## 段永平

不贵。生意模式还在，我还拿着。

千里牛 2018-08-03 11:55 回本文开头 ↑
"""


# 库里拆出来的整篇导航。⽂ 是 U+2F42 康熙部首，不是普通的文。
KANGXI_SHELL = "尼古拉 ·特斯拉 2010-11-08 18:15 回本 ⽂开头 ↑"
PLAIN_SHELL = "千里牛 2010-11-08 18:15 回本文开头 ↑"


def test_kangxi_and_plain_nav_shells_are_chrome_and_empty():
    assert any(ord(char) == 0x2F42 for char in KANGXI_SHELL)
    assert "\u6587" not in KANGXI_SHELL
    assert "\u2f42" not in normalize_voice(KANGXI_SHELL)
    assert "文" in normalize_voice(KANGXI_SHELL)
    assert is_chrome_line(KANGXI_SHELL)
    assert voice_is_empty(KANGXI_SHELL)
    assert substantive_voice(KANGXI_SHELL) == ""
    assert stored_answer_empty(KANGXI_SHELL)
    assert stored_answer_empty(KANGXI_SHELL, source_id="duanyongping-blog")
    assert stored_answer_empty(PLAIN_SHELL)
    assert stored_answer_empty("尼古拉 ·特斯拉 2010-11-08 18:15\n回本 \u2f42开头 ↑")
    assert is_chrome_line("回本\u200b文开头 ↑")
    assert stored_answer_empty("回本文开头")
    assert stored_answer_empty("回 本 文 开 头 ↑")
    assert is_chrome_line(PLAIN_SHELL)
    wrapped = (
        "---\n"
        f'title: "{KANGXI_SHELL}"\n'
        "author_voice: all\n"
        "off_timeline: true\n"
        "---\n\n"
        f"{KANGXI_SHELL}\n"
    )
    assert stored_answer_empty(wrapped)
    assert stored_answer_empty("---\ntitle: x\noff_timeline: true\n---\n", title=KANGXI_SHELL)
    # 有正文的博文不是空回答。句子里带「回到」「开头」也不算导航壳。
    assert not stored_answer_empty("能力圈就是不做不懂的事。苹果只是举例。")
    assert not stored_answer_empty("不贵。生意模式还在，我还拿着。\n\n" + PLAIN_SHELL)
    assert not stored_answer_empty("回到苹果的生意模式，我还拿着。开头那几年看不懂，后来利润起来了。")
    assert not stored_answer_empty("---\ntitle: x\n---\n", title="苹果的生意模式还在，我一直拿着不卖。")
    # 整篇导航壳只搁段永平博客。别的来源同样的一行不标。
    assert stored_answer_empty(KANGXI_SHELL, source_id="fangwei-zsxq") is False
    assert hide_empty_answer(None, 15079, KANGXI_SHELL, {"off_timeline": True}, source_id="fangwei-mp") is False
    assert stored_answer_empty("## 段永平\n\n" + KANGXI_SHELL)
    assert "看懂了" in substantive_voice("苹果2018年的生意模式我看懂了，所以一直拿着。")


def test_chrome_only_voice_is_empty_and_real_answer_stays():
    assert voice_is_empty("千里牛 2018-08-03 11:55 回本文开头 ↑")
    assert voice_is_empty("李历 2018-08-05 23:48 回本文开头 ↑")
    assert voice_is_empty("回本文开头")
    assert voice_is_empty("回 本 文 开 头 ↑")
    assert not voice_is_empty("不贵。生意模式还在，我还拿着。\n\n千里牛 2018-08-03 11:55 回本文开头 ↑")
    assert "回本文开头" not in substantive_voice("不贵。生意模式还在。\n千里牛 2018-08-03 11:55 回本文开头 ↑")
    assert "生意模式" in substantive_voice("不贵。生意模式还在。\n千里牛 2018-08-03 11:55 回本文开头 ↑")
    # 句子里的日期不是导航行。
    assert "看懂了" in substantive_voice("苹果2018年的生意模式我看懂了，所以一直拿着。")
    assert stored_answer_empty(COOK)
    assert stored_answer_empty(PDD)
    assert not stored_answer_empty(REAL)
    assert not stored_answer_empty("能力圈就是不做不懂的事。苹果只是举例。")


def test_suppress_stays_on_duanyongping_and_does_not_touch_other_sources():
    guest = "方伟读者：茅台现在太贵了吗？我觉得生意模式还在，准备继续拿着。\n"
    assert stored_answer_empty(guest) is False
    # 别的来源即使贴了同样的问答标题，也不搁下，也不碰数据库。
    assert hide_empty_answer(None, 1, COOK, {}, source_id="fangwei-zsxq") is False
    assert hide_empty_answer(None, 1, COOK, {}, source_id="fangwei-mp") is False


def test_classify_drops_chrome_even_when_the_question_names_a_company():
    post = Post(
        title="库克的信",
        date="2018-08-03 11:55:00",
        repost=False,
        blocks=[
            Block("网友：苹果市值到了一万亿，蒂姆·库克写了内部信，生意模式还成立吗？", False, 12, 1),
            Block("千里牛 2018-08-03 11:55 回本文开头 ↑", True, 12, 1),
        ],
        page=1,
    )
    entries = [classify(item) for item in segment_post(post)]
    assert entries
    assert all(not entry.keep for entry in entries)
    assert any(entry.reason == "只有论坛导航，没有段永平的话" for entry in entries)

    kept_post = Post(
        title="苹果",
        date="2018-08-04 09:00:00",
        repost=False,
        blocks=[
            Block("网友：苹果现在贵不贵？", False, 12, 1),
            Block("不贵。生意模式还在，我还拿着。", True, 12, 1),
            Block("千里牛 2018-08-04 09:10 回本文开头 ↑", False, 12, 1),
        ],
        page=1,
    )
    kept = [entry for entry in (classify(item) for item in segment_post(kept_post)) if entry.keep]
    assert len(kept) == 1
    assert kept[0].companies == ["苹果"]
    rendered = render_markdown(kept[0])
    assert "生意模式还在" in rendered
    assert "回本文开头" not in rendered


def test_reclassify_decision_hides_chrome_and_keeps_a_real_reply():
    empty = decide_stored("库克万亿内部信", COOK)
    assert empty["suppressed"] is True
    assert empty["off_views"] is True
    assert empty["reason"] == "只有论坛导航，没有段永平的话"
    pdd = decide_stored("拼多多到底能不能投", PDD)
    assert pdd["suppressed"] is True
    real = decide_stored("苹果贵不贵", REAL)
    assert real.get("suppressed") is not True
    assert real["off_views"] is False
    assert "苹果" in real["reason"]


def test_model_input_skips_the_forum_line():
    text = "段：不贵。生意模式还在。\n千里牛 2018-08-03 11:55 回本文开头 ↑\n博友：我觉得该卖。"
    assert duan_paragraphs(text) == "不贵。生意模式还在。"


def test_qa_layout_splits_the_question_and_hides_chrome():
    html = render_qa_html(COOK)
    assert html is not None
    assert "qa-ask" in html and "qa-answer" in html
    assert "没有有效回答" in html
    assert "回本文开头" not in html
    assert "千里牛" not in html
    ask = html.split("qa-answer", 1)[0]
    assert ask.count("<p>") >= 2
    assert "dollars. He" in html
    assert "one trillion" in html
    assert "一万亿美元" in html

    real = render_qa_html(REAL)
    assert real is not None
    assert "没有有效回答" not in real
    assert "回本文开头" not in real
    assert "生意模式还在" in real
    assert "苹果现在贵不贵" in real
    spaced = render_qa_html("## 读者问\n\n苹 果 现 在 贵 不 贵 ？\n\n## 段永平\n\n不贵。生意模式还在，我还拿着。\n")
    assert spaced is not None and "苹果现在贵不贵" in spaced
    assert "<h2>" not in (real or "")

    page = prepare_body_html(PDD, "拼多多到底能不能投", {}, None, None)
    assert "qa-ask" in page
    assert "没有有效回答" in page
    assert "回本文开头" not in page
    plain = prepare_body_html("这是一段普通正文，没有问答标题。\n", "普通", {}, None, None)
    assert "qa-block" not in plain

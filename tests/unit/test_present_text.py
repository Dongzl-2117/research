"""阅读页展示层：对话标签统一、明显错字、星球问答标题。"""

from pathlib import Path

from researchlib.boilerplate import clean_body, search_text
from researchlib.mdlinks import prepare_body_html
from researchlib.present_text import (
    expand_reply_chain_line,
    merge_wrapped_utterances,
    normalize_dialogue_labels,
    order_nested_replies,
    plain_qa_markdown,
    polish_display_text,
    present_article_text,
    present_zsxq_qa,
)
from researchlib.render import render_body

_CSS = Path(__file__).resolve().parents[2] / "src" / "researchlib" / "web" / "static" / "site.css"

# 形状对齐文章 24006：标题是回答的第一句，正文另有截断的「# 问：」，
# 再加「## 提问（…）」「## 回答（…）」。名字用虚构的，星主仍写方伟。
_QA_24006 = """# AI算力是一个很新都东西

# 问：星主好，想请教目前腾讯这笔投入以后怎么收回来，回报能不能超过基本的ROIC(…

- 作者：王小明
- 时间：2026-10-27 09:55 (UTC+8)
- 类型：q&a
- 点赞 14 · 评论 0 · 阅读人数 475
- topic_id：`55188` · 原链接：https://wx.zsxq.com/topic/55188
- 原始数据： raw/topics/55188.json

## 提问（王小明 → 方伟Rey）

星主好，想请教目前腾讯这笔投入以后怎么收回来，回报能不能超过基本的ROIC(暂按10%)？谢谢

## 回答（方伟Rey）

AI算力是一个很新都东西，其实你看中美科技巨头也在被迫回答这个问题。

而且，他们的商业模式决定了他们会把算力留给自己尝试
"""
_QA_TITLE = "AI算力是一个很新都东西"


def test_legacy_comment_labels_become_fangwei_wangyou():
    raw = "## 评论里的回复\n\n读者问：奈飞怎么样？\n\n作者答：运营杠杆还在。\n\n作者补充：再看一眼广告。\n"
    out = normalize_dialogue_labels(raw)
    assert "网友：奈飞怎么样？" in out
    assert "方伟：运营杠杆还在。" in out
    assert "方伟：再看一眼广告。" in out
    assert "读者问" not in out and "作者答" not in out and "作者补充" not in out


def test_polish_fixes_obvious_typos_only():
    assert polish_display_text("AI算力是一个很新都东西") == "AI算力是一个很新的东西"
    assert polish_display_text("存在运用杠杆的好处") == "存在运营杠杆的好处"
    # 口语不动
    assert polish_display_text("我觉得还行吧") == "我觉得还行吧"


def test_youtube_netflix_zsxq_fixture_reads_as_one_dialogue_skin():
    """23999：正文短句 + 评论里的回复旧标签。问答标题转换不能把这一节吃掉。"""
    body = """YouTube的护城河类型是平台网络效应，Netflix只是规模效应，前者比后者强

## 评论里的回复

读者问：奈飞生意其实很不错的，每年内容成本固定200亿。

作者答：这里面很多人误会的应该是觉得YouTube的成本比Netflix低，其实不然，YouTube是可变成本低，它是一半要分给创作者，Netflix是存在运用杠杆的好处。
"""
    html = str(render_body(body, {}))
    assert "读者问" not in html and "作者答" not in html
    assert '<span class="who">网友：</span>奈飞生意其实很不错的' in html
    assert '<span class="who">方伟：</span>这里面很多人误会的' in html
    assert "方伟：YouTube的护城河" not in html
    assert "存在运营杠杆的好处" in html
    assert "<h2>评论里的回复</h2>" in html
    assert "提问（" not in html and "回答（" not in html
    # 评论区不是另一套 zsxq-comment 列表，而是与正文同一段落对话体
    assert "zsxq-comment-author" not in html
    assert html.count('class="who"') == 2


def test_present_article_text_composes_label_and_typo():
    text = "读者问：很新都东西\n\n作者答：存在运用杠杆"
    assert present_article_text(text) == "网友：很新的东西\n\n方伟：存在运营杠杆"


def test_24006_qa_headings_render_as_dialogue_paragraphs():
    """24006 这种问答导出：不要 h1/h2「问： / 提问 / 回答」，改成和 OCR 一样的 .who。"""
    cleaned = clean_body(_QA_24006)
    assert "topic_id" not in cleaned and "王小明" in cleaned
    html = prepare_body_html(cleaned, _QA_TITLE, {}, None, None)
    assert "<h1" not in html and "<h2" not in html
    assert "提问" not in html and "回答（" not in html and "问：" not in html
    assert "王小明" not in html and "方伟Rey" not in html
    assert '<span class="who">网友：</span>星主好，想请教目前腾讯这笔投入以后怎么收回来' in html
    assert "暂按10%" in html
    assert "ROIC(…" not in html
    assert '<span class="who">方伟：</span>AI算力是一个很新的东西，其实你看中美科技巨头也在被迫回答这个问题。' in html
    assert "很新都东西" not in html
    assert "而且，他们的商业模式决定了他们会把算力留给自己尝试" in html
    assert "方伟：而且" not in html
    assert html.count('class="who"') == 2
    assert present_zsxq_qa(present_zsxq_qa(cleaned)) == present_zsxq_qa(cleaned)


def test_plain_qa_markdown_keeps_the_asker_name_out():
    """接口给的原文和检索文字：问答小标题只留「提问」「回答」，归档头里的作者（提问的网友）不要，别的行照旧。"""
    plain = plain_qa_markdown(_QA_24006)
    assert "王小明" not in plain and "方伟Rey" not in plain
    expected = (
        _QA_24006.replace("- 作者：王小明\n", "")
        .replace("## 提问（王小明 → 方伟Rey）", "## 提问")
        .replace("## 回答（方伟Rey）", "## 回答")
    )
    assert plain == expected
    assert plain_qa_markdown(plain) == plain
    text = search_text(clean_body(_QA_24006), _QA_TITLE)
    assert "王小明" not in text and "方伟Rey" not in text
    assert "\n提问\n" in text and "\n回答\n" in text
    # 提问以后的「作者：」是帖子的话，不动。
    quoted = _QA_24006.replace("而且，", "- 作者：李四\n\n而且，")
    assert "- 作者：李四" in plain_qa_markdown(quoted)
    # 不是问答导出（只有一种小标题、在代码块里）原样返回，归档头也不动。
    notes = "# 读书笔记\n\n- 作者：王小明\n\n## 回答（续）\n\n上回没说完的。\n"
    assert plain_qa_markdown(notes) == notes
    fenced = "- 作者：王小明\n\n```\n## 提问（王小明 → 方伟Rey）\n\n## 回答（方伟Rey）\n```\n"
    assert plain_qa_markdown(fenced) == fenced


def test_qa_keeps_comment_replies_and_host_comment_list():
    """问答正文换成对话之后，评论里的回复和「评论（N）」列表都还在。"""
    body = """# 问：为什么供应链做不出差异

## 提问（王小明 → 方伟Rey）

为什么供应链做不出差异

## 回答（方伟Rey）

下图

![](shot.jpg)

## 评论里的回复

读者问：差异化是不是只能做在服务上？

作者答：服务又跟人力成本绑在一起。

## 评论（1）

- **方伟Rey**（2026-01-09 15:07）
  再补一句。
"""
    html = str(render_body(body, {"shot.jpg": "/media/fangwei-zsxq/ab/shot.jpg"}))
    assert "<h1" not in html
    assert "提问（" not in html and "回答（" not in html
    assert '<span class="who">网友：</span>为什么供应链做不出差异' in html
    assert '<span class="who">方伟：</span>下图' in html
    assert 'src="/media/fangwei-zsxq/ab/shot.jpg"' in html
    assert "<h2>评论里的回复</h2>" in html
    assert '<span class="who">网友：</span>差异化是不是只能做在服务上？' in html
    assert '<span class="who">方伟：</span>服务又跟人力成本绑在一起。' in html
    assert "读者问" not in html and "作者答" not in html
    assert 'class="zsxq-comment-author">方伟Rey<' in html
    assert "再补一句" in html
    assert html.index("评论里的回复") < html.index("zsxq-comments")


def test_title_wen_and_longer_duplicate_wen_both_drop():
    """标题本身是截短的「问：」，正文里还有一行更长的「问：」重复提问。"""
    body = """# 问：Rey，你会用期权吗

# 问：Rey，你会用期权吗，比直接卖 put

## 提问（王小明 → 方伟Rey）

Rey，你会用期权吗，比直接卖 put

## 回答（方伟Rey）

可买可不买的时候再看。
"""
    html = prepare_body_html(body, "问：Rey，你会用期权吗", {}, None, None)
    assert "<h1" not in html and "<h2" not in html
    assert html.count("比直接卖 put") == 1
    assert '<span class="who">网友：</span>Rey，你会用期权吗，比直接卖 put' in html
    assert '<span class="who">方伟：</span>可买可不买的时候再看。' in html


def test_wen_heading_that_is_not_the_question_stays():
    body = """# 问：先看这张表

## 提问（王小明 → 方伟Rey）

利润率以后怎么走？

## 回答（方伟Rey）

还要看投放。
"""
    html = str(render_body(body, {}))
    assert "<h1>问：先看这张表</h1>" in html
    assert '<span class="who">网友：</span>利润率以后怎么走？' in html
    assert '<span class="who">方伟：</span>还要看投放。' in html
    assert "提问（" not in html


def test_plain_headings_and_fenced_qa_are_left_alone():
    prose = "他在回答里提到提问。\n\n## 回答的框架\n\n先看回报。\n"
    assert present_zsxq_qa(prose) == prose
    bare = "## 提问\n\n怎么看？\n\n## 回答\n\n还行。\n"
    assert present_zsxq_qa(bare) == bare
    fenced = (
        "说明\n\n```\n## 提问（王小明 → 方伟Rey）\n\n怎么看？\n\n## 回答（方伟Rey）\n\n还行。\n```\n"
    )
    assert present_zsxq_qa(fenced) == fenced
    crlf = "提问和回答都出现了，但不是标题。\r\n下一行。\r\n"
    assert present_zsxq_qa(crlf) == crlf


def test_ocr_blocks_inherit_prose_font_without_grey_box():
    css = _CSS.read_text(encoding="utf-8")
    rules = css.split(".prose .ocr-reading {", 1)[1].split(".prose .ocr-links {", 1)[0]
    assert "font-size: 15px" not in rules
    assert "var(--bg-sunk)" not in rules
    assert "background: var(--surface)" not in rules
    assert "font-size: inherit" in rules
    assert "font-weight: inherit" in rules
    assert "line-height: inherit" in rules
    assert "color: inherit" in rules
    links = css.split(".prose .ocr-links a {", 1)[1].split("}", 1)[0]
    assert "font-size: 13px" in links


def test_prose_links_use_blue_but_ocr_reading_keeps_text_color():
    """正文外链回到蓝链；识图转写整段都是链接，仍跟周围文字同色。"""
    css = _CSS.read_text(encoding="utf-8")
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    dark = css.split('[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    assert "--link:" in root and "--link-visited:" in root
    assert "--link:" in dark and "--link-visited:" in dark
    rule = css.split(".prose a {", 1)[1].split("}", 1)[0]
    visited = css.split(".prose a:visited {", 1)[1].split("}", 1)[0]
    hover = css.split(".prose a:hover {", 1)[1].split("}", 1)[0]
    assert "color: var(--link)" in rule
    assert "text-decoration: underline" in rule
    assert "currentColor" in rule
    assert "var(--accent)" not in rule
    assert "color: var(--link-visited)" in visited
    assert "var(--accent)" not in hover
    assert "currentColor" in hover
    # 识图转写的字本身是链到原图的链接：不刷蓝，靠淡下划线辨认。
    ocr = css.split(".prose .ocr-reading a {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in ocr
    assert "text-decoration: underline" in ocr
    ocr_visited = css.split(".prose .ocr-reading a:visited {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in ocr_visited
    ocr_hover = css.split(".prose .ocr-reading a:hover {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in ocr_hover
    # 表格整张也是链接：比上面那条更具体，仍不画下划线。
    table = css.split(".prose .ocr-reading a.ocr-table-link {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in table
    assert "text-decoration: none" in table
    # 「原图 N」仍是小一号的控件，不跟正文句子抢颜色。
    original = css.split(".prose .ocr-links a {", 1)[1].split("}", 1)[0]
    assert "var(--ink-3)" in original
    original_hover = css.split(".prose .ocr-links a:hover {", 1)[1].split("}", 1)[0]
    assert "var(--accent)" not in original_hover


# 文章 24008 方洪波视频截图：模型按画面折行，一句拆成多行。
_FANG_LINES = """方洪波：
企业家精神是敢于在最顺的时候
对自己动刀子
光做家电
没有未来
我们光做家电没有未来
that just making home appliances offered no future.
转型本质就是转人，在企业最顺的时候对自己动刀子 (1/3)
"""

# 文章 24008 雪球截图：上面是较晚的回复，下面卡片是更早的引语。昵称已收成网友。
_XUEQIU_VISUAL = (
    "网友：李录投资也很好，我们投资的基本理念其实非常接近，能力圈可能有点不同而已。"
    "目前我不会考虑和任何人做任何节目的。\n"
    "网友：大道会不会考虑和李录做一期对谈节目？\n"
    "网友：莫尼什：我是苹果用户。段永平拥有OPPO和vivo，也有一个很大的股票投资组合。"
    "他最大的持仓是苹果，直接竞争对手。他是我的朋友。你知道他怎么跟我说的吗？他…\n"
)


def test_merge_wrapped_quote_into_paragraphs():
    """只接半句折行。模型分成多行的标题、字幕不收成一整段。"""
    out = merge_wrapped_utterances(_FANG_LINES)
    assert "企业家精神是敢于在最顺的时候对自己动刀子" in out
    assert "时候\n对自己" not in out
    assert "光做家电\n没有未来" in out
    assert "我们光做家电没有未来\nthat just making" in out
    assert "对自己动刀子\n\n光做" not in out
    assert "刀子光做家电" not in out
    assert "future.\n转型本质" in out
    assert out.index("企业家精神") < out.index("光做家电") < out.index("转型本质")
    assert merge_wrapped_utterances(out) == out.strip()


def test_merge_keeps_blank_lines_the_model_wrote():
    """识图用空行分的段原样留下，一段里的两句也不拆开。"""
    written = (
        "方洪波：企业家精神是敢于在最顺的时候对自己动刀子\n\n"
        "光做家电没有未来\n\n"
        "我们光做家电没有未来。that just making home appliances offered no future.\n\n"
        "转型本质就是转人，在企业最顺的时候对自己动刀子 (1/3)"
    )
    assert merge_wrapped_utterances(written) == written
    one_line = "第一句完了。第二句也在这段里。"
    assert merge_wrapped_utterances(one_line) == one_line


def test_merge_leaves_dialogue_lines_and_long_notes():
    dialogue = "方伟：第一句还在观察同店。\n网友：第二句先不提价。"
    assert merge_wrapped_utterances(dialogue) == dialogue
    headlines = "浙江绍兴会稽山 首度超越古越龙山\n2026年上半年营业收入达8.53亿元\n江浙沪的销售额高达70%以上"
    merged = merge_wrapped_utterances(headlines)
    assert "古越龙山2026" not in merged
    assert "亿元江浙沪" not in merged
    assert "8.53亿" in merged


# 文章截图按屏幕宽度一行一行抄下来：行行差不多一样宽，断在词中间（文章 10503 是这样）。
_SCREEN_WRAPPED = """很多人把开店当成增长，其实新店要先分清楚哪些是
资产（asset），哪些只是费用（cost）：房租、
人工和装修摊销加起来，单店每月至少要卖两万杯才
能打平，而同店销量已经连续三个季度下滑，新店又
在分流老店的客人，所以扩张越快利润越薄。
二、怎么看估值
按明年的利润算市盈率不到十五倍，看起来很便宜，
但如果同店继续下滑，利润预测本身就要往下调，便
宜只是表面上的。"""


def test_merge_rejoins_lines_wrapped_at_screen_width():
    """排满的行接成整段，小标题不粘进正文；全角括号贴着汉字，不空格。再跑一遍不变。"""
    out = merge_wrapped_utterances(_SCREEN_WRAPPED)
    assert out.splitlines() == [
        "很多人把开店当成增长，其实新店要先分清楚哪些是资产（asset），哪些只是费用（cost）："
        "房租、人工和装修摊销加起来，单店每月至少要卖两万杯才能打平，"
        "而同店销量已经连续三个季度下滑，新店又在分流老店的客人，所以扩张越快利润越薄。",
        "二、怎么看估值",
        "按明年的利润算市盈率不到十五倍，看起来很便宜，但如果同店继续下滑，利润预测本身就要往下调，"
        "便宜只是表面上的。",
    ]
    assert merge_wrapped_utterances(out) == out


def test_merge_keeps_slides_and_list_items_apart():
    """一行一条的幻灯片要点（没有逗号句号）不接；编号的条目不粘到上一行，条目自己的折行照接。"""
    slides = (
        "毛利率提升至百分之六十二点五创历史新高\n"
        "经营利润率提升至百分之三十一同比增加五个点\n"
        "自由现金流达到四百亿元同比增长百分之四十\n"
        "回购和分红合计三百亿元占自由现金流七成"
    )
    assert merge_wrapped_utterances(slides) == slides
    items = (
        "我喜欢的公司往往必须同时具备几个特点：\n"
        "1、竞争优势巨大（我的要求是必须远胜于行业第二\n"
        "名）且有清晰壁垒；\n"
        "2、管理层诚信、有长期主义，不会为了短期的数字\n"
        "去损害长期的竞争力；\n"
        "3、估值合理，最好是在市场悲观的时候买到，这样\n"
        "安全边际才足够。"
    )
    assert merge_wrapped_utterances(items).splitlines() == [
        "我喜欢的公司往往必须同时具备几个特点：",
        "1、竞争优势巨大（我的要求是必须远胜于行业第二名）且有清晰壁垒；",
        "2、管理层诚信、有长期主义，不会为了短期的数字去损害长期的竞争力；",
        "3、估值合理，最好是在市场悲观的时候买到，这样安全边际才足够。",
    ]
    table = "项目 2024 2025 2026\n营业收入 1200 1500 1800\n经营利润 300 420 560\n净利润 280 390 520"
    assert merge_wrapped_utterances(table) == table


def test_xueqiu_visual_order_becomes_chronological():
    out = order_nested_replies(_XUEQIU_VISUAL)
    assert out.index("莫尼什：") < out.index("大道会不会考虑") < out.index("李录投资也很好")
    assert order_nested_replies(out) == out
    plain = "网友：先看基本面，别急着动手。\n网友：你确定吗？\n网友：我再想想，明天再说。"
    assert order_nested_replies(plain) == plain
    mixed = "网友：大道会不会考虑？\n方伟：先不谈。\n网友：莫尼什：我是苹果用户，这段引用要很长才像卡片。"
    assert order_nested_replies(mixed) == mixed


def test_reply_chain_expands_earlier_speech_first():
    line = (
        "回复@王小明: 李录投资也很好，目前我不会考虑和任何人做任何节目的。"
        "//@王小明:回复@李四:大道会不会考虑和李录做一期对谈节目？"
    )
    expanded = expand_reply_chain_line(line)
    assert expanded == [
        "网友：大道会不会考虑和李录做一期对谈节目？",
        "网友：李录投资也很好，目前我不会考虑和任何人做任何节目的。",
    ]
    with_card = line + "\n网友：莫尼什：我是苹果用户。段永平拥有OPPO和vivo，持仓很大。"
    ordered = order_nested_replies(with_card)
    assert ordered.index("莫尼什：") < ordered.index("大道会不会") < ordered.index("李录投资也很好")
    assert "回复@" not in ordered and "//@" not in ordered and "王小明" not in ordered
    assert order_nested_replies(ordered) == ordered

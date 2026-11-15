import random
from html.parser import HTMLParser

import pytest

from researchlib.ocr import append_ocr, compose_ocr_text
from researchlib.ocr_units import ImageText, normalize_units
from researchlib.render import (
    _PHRASE,
    _phrase_overlap,
    image_anchor,
    inline_image_ocr,
    lazy_images,
    render_body,
    sanitize_html,
)
from researchlib.snippets import answer_html, highlight_snippet
from researchlib.textutil import argumentative_excerpt, markdown_to_text


def test_snippet_keeps_a_late_hit():
    body = ("前言。" * 40) + "这里才提到腾讯。" + ("后记。" * 10)
    rendered = str(highlight_snippet(body, "腾讯"))
    assert rendered.startswith("…")
    assert "<mark>腾讯</mark>" in rendered


def test_snippet_window_goes_where_the_rare_words_are():
    # 开头的语录里有「商业」，后面整段在讲开市客：窗口放在开市客那里，不停在第一处碰到的常见词上。
    body = (
        "“投资要情绪稳定，在投资和商业决策中要理性。”——某位投资人。" + "闲话。" * 40
        + "开市客是会员的采购代理人，开市客把毛利率压在 14% 以下，会员费才是利润。" + "后记。" * 30
    )
    terms = ["开市客", "Costco", "商业", "模式"]
    groups = [(5.0, ["开市客", "Costco"]), (2.4, ["商业"]), (2.3, ["模式"])]
    first_hit = str(highlight_snippet(body, "Costco 商业模式", terms=terms))
    assert "<mark>商业</mark>决策" in first_hit and "开市客" not in first_hit
    placed = str(highlight_snippet(body, "Costco 商业模式", terms=terms, groups=groups))
    assert "<mark>开市客</mark>是会员的采购代理人" in placed
    assert "决策" not in placed


def test_snippet_window_prefers_more_of_the_question_then_the_earlier_place():
    body = "模式。" + "甲" * 120 + "商业模式和开市客。" + "乙" * 120 + "开市客、开市客、开市客。"
    groups = [(5.0, ["开市客"]), (2.4, ["商业"]), (2.3, ["模式"])]
    placed = str(highlight_snippet(body, "", terms=["开市客", "商业", "模式"], groups=groups, radius=60))
    # 三个词组都在的那处比只有公司名、但出现三次的那处好。
    assert "<mark>商业模式</mark>和<mark>开市客</mark>" in placed
    # 写到的词一样多时取靠前的，不挑把词重复了几遍的地方。
    body = "护城河是企业抵御竞争、守住利润的结构性优势。" + "甲" * 120 + "护城河、护城河、护城河。"
    placed = str(highlight_snippet(body, "", terms=["护城河"], groups=[(3.0, ["护城河"])], radius=60))
    assert "抵御竞争" in placed and "护城河、" not in placed


def test_snippet_escapes_and_marks():
    snippet = highlight_snippet("<script>alert(1)</script> 腾讯广告", "腾讯", radius=80)
    rendered = str(snippet)
    assert "<script>" not in rendered
    assert "<mark>腾讯</mark>" in rendered
    assert "&lt;" in rendered


def test_markdown_strips_script_and_rewrites_image():
    html = render_body(
        '正文有腾讯。\n\n![](pic.png)\n\n<script>alert(1)</script>\n<img src="x" onerror="alert(1)">',
        {"pic.png": "/media/fangwei-mp/ab/pic.png"},
    )
    text = str(html)
    assert "<script>" not in text
    assert "onerror" not in text
    assert 'src="/media/fangwei-mp/ab/pic.png"' in text
    assert "腾讯" in text


def test_external_image_is_not_hotlinked():
    html = str(render_body("![](https://example.com/secret.png)", {}))
    assert "example.com" not in html
    assert "外部图片已省略" in html


# feishu2md 把飞书的高亮块导成这样：引用块第一行是「[!NOTE]」。
CALLOUT = (
    "前言一句。\n\n"
    "> [!NOTE]\n> 石油企业的运行九成是政治，一成是石油。\n\n"
    "> [!tip] 能力圈有多大不要紧！\n\n"
    "正文继续。"
)


def test_callout_marker_is_not_part_of_the_text():
    text = markdown_to_text(CALLOUT)
    assert "[!" not in text and "NOTE" not in text and "tip" not in text
    assert "石油企业的运行九成是政治，一成是石油。" in text
    assert "能力圈有多大不要紧！" in text
    quote_first = CALLOUT.split("\n\n", 1)[1]
    assert argumentative_excerpt(markdown_to_text(quote_first)) == "石油企业的运行九成是政治，一成是石油。"


def test_image_file_names_and_empty_links_are_not_text():
    """检索文字里以前夹着飞书图片的文件名、Stratechery 包在图外的「[](大图地址)」。"""
    feishu = (
        "价格带分化加剧。\n\n![image.png](https://example.com/media/a.png)\n\n"
        "![截屏2025-06-08 19.36.10.png](https://example.com/media/b.png)\n\n"
        "![IMG_8718.JPG](https://example.com/media/c.jpg)\n\n品牌还在。"
    )
    assert markdown_to_text(feishu) == "价格带分化加剧。\n\n品牌还在。"
    stratechery = (
        "goes deeper than hardware sales.\n\n"
        "[](https://example.com/wp-content/uploads/2017/06/Screen-Shot-2017-06-01-at-7.40.43-PM.png?ssl=1)\n\n"
        "![Screen Shot 2017-06-01 at 7.40.43 PM](https://example.com/Screen-Shot.png?resize=640%2C478&ssl=1)\n\n"
        "[![img0110](https://example.com/IMG0110.png?ssl=1)](https://example.com/IMG0110.png)\n\n"
        "This is why India matters."
    )
    text = markdown_to_text(stratechery)
    assert "png" not in text and "Screen" not in text and "img0110" not in text and "](" not in text
    assert text.startswith("goes deeper than hardware sales.") and text.endswith("This is why India matters.")
    # 说了图里是什么的替代文字、有字的链接照留。
    kept = markdown_to_text("![腾讯 2024 年收入结构](https://example.com/x.png) 见[年报](https://example.com/r.pdf)")
    assert kept == "腾讯 2024 年收入结构 见年报"
    assert markdown_to_text("![腾讯收入结构.png](https://example.com/x.png)") == "腾讯收入结构.png"
    assert markdown_to_text("![Revenue mix 2026.svg](https://example.com/x.svg)") == "Revenue mix 2026.svg"
    assert markdown_to_text("![" + "a" * 64 + ".png](https://example.com/x.png)") == ""


def test_times_sign_in_formulas_is_not_italics():
    """林文丰飞书的公式：「DAU*人均PV*Ad load」以前渲染成「DAU<em>人均PV</em>Ad load」，检索文字连下划线一起删光。"""
    formula = "1.Ad revenue =DAU*人均PV*Ad load*广告展现率*CPM，eCPM=CPA_bid*CTR*CVR*1000"
    html = str(render_body(formula, {}))
    assert "<em>" not in html and "DAU*人均PV*Ad load*广告展现率*CPM" in html and "CPA_bid*CTR*CVR*1000" in html
    assert "<em>" not in str(answer_html("按 DAU*人均PV*CPM 算"))
    assert markdown_to_text(formula) == formula
    assert markdown_to_text("eCPM = ￥2.0 * 0.015 * 1000，(广告加载率)*填充率，毛利率30%*收入") == (
        "eCPM = ￥2.0 * 0.015 * 1000，(广告加载率)*填充率，毛利率30%*收入"
    )
    # 真的斜体、加粗照旧；代码和链接地址里的不动。
    styled = "这是*重点*内容，are *not*: and **加粗**，代码 `a*b` 见[x](https://example.com/a*b)"
    html = str(render_body(styled, {}))
    assert "<em>重点</em>" in html and "<em>not</em>" in html and "<strong>加粗</strong>" in html
    assert "<code>a*b</code>" in html and 'href="https://example.com/a*b"' in html
    assert markdown_to_text(styled) == "这是重点内容，are not: and 加粗，代码 a*b 见x"
    # 词中间的下划线是字；分隔线不进文字。
    assert markdown_to_text("md5__1038 和 snake_case，_斜体_ 和 __加粗__\n\n* * *\n\n正文") == (
        "md5__1038 和 snake_case，斜体 和 加粗\n\n正文"
    )


def test_callout_renders_as_a_plain_quote():
    html = str(render_body(CALLOUT, {}))
    assert "[!" not in html and "NOTE" not in html
    assert "<blockquote>" in html
    assert "石油企业的运行九成是政治，一成是石油。" in html
    assert "能力圈有多大不要紧！" in html


def test_zsxq_hashtag_lines_are_not_headings():
    """1250 末尾的「#拼多多#」曾被渲染成一级标题。"""
    html = str(render_body("正文第一段。\n\n#拼多多#\n\n#拼多多# #Temu#\n\n今天聊 #拼多多# 的财报。\n", {}))
    assert "<h1" not in html
    assert "<p>#拼多多#</p>" in html and "#拼多多# #Temu#" in html and "今天聊 #拼多多# 的财报。" in html
    # 真的标题照旧，代码块里的不加反斜杠。
    assert "<h2>评论（1）</h2>" in str(render_body("## 评论（1）\n", {}))
    assert "\\#" not in str(render_body("```\n#拼多多#\n```\n", {}))


def test_sanitize_javascript_url():
    cleaned = sanitize_html('<a href="javascript:alert(1)">链接</a>')
    assert "javascript:" not in cleaned


class _Tags(HTMLParser):
    """记下浏览器会看到的标签和属性名。"""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((name, value or "") for name, value in attrs)


def _parsed(html: str) -> _Tags:
    tags = _Tags()
    tags.feed(html)
    tags.close()
    return tags


@pytest.mark.parametrize(
    "raw",
    [
        "<svg/onload=alert(1)>",
        "<a href=javascript:alert(1)>x</a>",
        '<a/href="javascript:alert(1)">x</a>',
        '<a href="jav&#x09;ascript:alert(1)">x</a>',
        '<a href="javascript&colon;alert(1)">x</a>',
        '<a href=" \x01javascript:alert(1)">x</a>',
        '<a href="data:text/html,x">x</a>',
        "<img src=x onerror=alert(1)>",
        '<img/src="/media/a.png"/onerror=alert(1)>',
        "<math><mi xlink:href=javascript:alert(1)>x</mi></math>",
        '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
        "<iframe src=https://example.com></iframe>",
        '<base href="https://example.com/"><meta http-equiv="refresh" content="0">',
        "<form action=https://example.com><input name=a><button>b</button></form>",
        "<details open ontoggle=alert(1)>x</details>",
    ],
)
def test_sanitize_leaves_no_script_hooks(raw):
    tags = _parsed(sanitize_html(raw))
    assert not {"script", "svg", "math", "iframe", "base", "meta", "form", "input"} & set(tags.tags)
    for name, value in tags.attrs:
        assert not name.startswith("on")
        assert "javascript" not in value.lower()
        assert not value.lower().startswith("data:")


def test_sanitize_keeps_layout_and_footnotes():
    html = str(render_body("| a | b |\n|:--|--:|\n| 1 | 2 |\n\n正文[^1]\n\n[^1]: 注释\n", {}))
    assert '<th style="text-align: left">a</th>' in html
    assert '<a class="footnote-ref" href="#fn:1">1</a>' in html
    assert '<li id="fn:1">' in html


def test_sanitize_keeps_text_and_safe_links():
    cleaned = sanitize_html(
        '<p>AT&T &amp; 1 < 2 <a href="https://example.com/?a=1&amp;b=2" target="_blank">外链</a>'
        '<a href="/articles/3">站内</a><a href="#p-2">锚点</a><a href="mailto:a@example.com">信</a></p>'
    )
    assert "AT&amp;T &amp; 1 &lt; 2" in cleaned
    assert '<a href="https://example.com/?a=1&amp;b=2">外链</a>' in cleaned
    assert '<a href="/articles/3">站内</a>' in cleaned
    assert '<a href="#p-2">锚点</a>' in cleaned
    assert '<a href="mailto:a@example.com">信</a>' in cleaned


def test_sanitize_images_only_from_this_site():
    assert sanitize_html('<img src="/media/x/ab.png" alt="图">') == '<img src="/media/x/ab.png" alt="图">'
    assert sanitize_html('<img src="https://example.com/a.png">') == ""
    assert sanitize_html('<img src="//example.com/a.png">') == ""


def test_sanitize_style_drops_loading_and_overlays():
    cleaned = sanitize_html(
        '<p style="color: red; background: url(https://example.com/t.gif); position: fixed;'
        ' top: 0; width: expression(alert(1)); font-size: 15px">字</p>'
    )
    assert cleaned == '<p style="color: red; top: 0; font-size: 15px">字</p>'


def test_sanitize_unknown_tags_keep_text_and_stray_closers_are_dropped():
    cleaned = sanitize_html("<mp-style-type></mp-style-type><section><h-char>，</h-char>字</section></div></main><p>后")
    assert cleaned == "<section>，字</section><p>后</p>"
    assert sanitize_html("<!-- 注释 --><p>x</p><style>p{}</style><script>alert(1)</script>") == "<p>x</p>"


CHAT = (
    "微信\n20:41\n张三\n瑞幸和古茗都算头部，但瑞幸同店还有压力\n"
    "李四\n古茗店效还行，瑞幸的折扣不能再深了\n张三\n先看到这里，不展开了\n"
)
# 同一张截图，识图交回的结构：谁说的由看得见图的模型标好，界面字不在里面。
CHAT_UNITS = normalize_units(
    {
        "kind": "chat",
        "blocks": [
            {"type": "message", "speaker": "张三", "text": "瑞幸和古茗都算头部，但瑞幸同店还有压力"},
            {"type": "message", "speaker": "李四", "text": "古茗店效还行，瑞幸的折扣不能再深了"},
            {"type": "message", "speaker": "张三", "text": "先看到这里，不展开了"},
        ],
    }
)


def test_ask_prompt_includes_an_ocr_chunk_beside_the_body():
    """问这篇吃的是检索块。图片文字块进资料，不靠阅读页 HTML。"""
    from researchlib.ask import build_messages

    body = "瑞幸古茗是比较头部的。"
    ocr = compose_ocr_text([ImageText("ab" * 32, CHAT, CHAT_UNITS)])
    passages = [
        {
            "n": 1,
            "title": "图片",
            "author": "方伟",
            "source": "方伟知识星球",
            "date": "2026-10-18",
            "heading": "",
            "text": body,
            "before": "",
            "after": "",
        },
        {
            "n": 2,
            "title": "图片",
            "author": "方伟",
            "source": "方伟知识星球",
            "date": "2026-10-18",
            "heading": "图片里的字",
            "text": ocr,
            "before": "",
            "after": "",
        },
    ]
    prompt = build_messages("同店怎么样", passages)[1]["content"]
    assert body in prompt and "图片里的字" in prompt
    assert "张三：" in prompt and "瑞幸和古茗都算头部" in prompt
    assert prompt.index(body) < prompt.index("图片里的字")


def test_bilingual_pairs_keep_images_when_ocr_texts_are_passed():
    from researchlib.translate.display import article_pairs

    digest = "ab" * 32
    img = f'<img id="{image_anchor(digest)}" src="/media/stratechery/ab/{digest}.png" alt="">'
    en = f"<p>Aggregation still compounds.</p><p>{img}</p>"
    zh = "<p>聚合仍在复利。</p><p>图留在原文这一侧。</p>"
    shown = article_pairs(en, zh, ocr_texts={digest: CHAT}, host="Ben Thompson")
    html = "".join((pair.en or "") + (pair.zh or "") for pair in shown.pairs)
    assert f'<img id="{image_anchor(digest)}"' in html
    assert "ocr-reading" not in html and "ocr-link" not in html and '<span class="who">' not in html
    assert "瑞幸和古茗都算头部" not in html
    assert "Aggregation still compounds." in html and "聚合仍在复利。" in html


def test_body_speaker_label_with_a_nickname_is_bold():
    """正文行首的「昵称：」和「方伟：」一样加粗；来源、结论这类元信息词不是说话人。"""
    page = str(render_body("春风十里我自在：一吨就是2000瓶。\n", {}))
    assert '<span class="who">春风十里我自在：</span>一吨就是2000瓶。' in page
    plain = str(render_body("来源：思想×岭南\n\n结论：先看基本面。\n", {}))
    assert 'class="who"' not in plain


def test_chat_screenshot_stays_an_image_and_models_still_read_the_ocr():
    """14886 这种：正文一句，群聊在截图里。阅读页留原图；整理后的对话只进模型输入。"""
    digest = "ab" * 32
    html = str(
        render_body(
            "瑞幸古茗是比较头部的。\n\n![](chat.png)\n",
            {"chat.png": f"/media/fangwei-zsxq/ab/{digest}.png"},
        )
    )
    page = inline_image_ocr(html, {digest: CHAT})
    assert page == html
    assert "瑞幸古茗是比较头部的" in page
    assert f'id="{image_anchor(digest)}"' in page
    assert f'src="/media/fangwei-zsxq/ab/{digest}.png"' in page
    assert "ocr-link" not in page and "ocr-reading" not in page and "ocr-shot" not in page
    assert '<span class="who">' not in page
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" not in page
    assert "古茗店效还行" not in page
    presented = compose_ocr_text([ImageText(digest, CHAT, CHAT_UNITS)])
    model = append_ocr("瑞幸古茗是比较头部的。", presented)
    assert "【图片里的字】" in model
    assert model.index("瑞幸古茗是比较头部的") < model.index("【图片里的字】")
    assert "张三：" in presented and "李四：" in presented
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" in model
    assert "古茗店效还行" in model
    assert "方伟：" not in presented


def test_short_or_empty_ocr_keeps_the_image():
    digest = "cd" * 32
    html = str(render_body("![](logo.png)\n", {"logo.png": f"/media/x/cd/{digest}.png"}))
    page = inline_image_ocr(html, {digest: "logo"})
    assert f'<img id="{image_anchor(digest)}"' in page
    assert "点击看原图" not in page
    assert inline_image_ocr(html, {}) == html


def test_ocr_prose_stays_out_of_the_reading_html():
    """识图正文里的标签不能进阅读页。模型仍读到原文两段。"""
    digest = "ef" * 32
    html = str(render_body("![](note.png)\n", {"note.png": f"/media/x/ef/{digest}.png"}))
    prose = (
        "<script>alert(1)</script>这是贴成图片的一段长文，讲渠道反馈和批价。\n"
        "第二段继续写机制，不是一行一句的聊天记录。"
    )
    page = inline_image_ocr(html, {digest: prose})
    assert page == html
    assert f'<img id="{image_anchor(digest)}"' in page
    assert "<script>" not in page and "渠道反馈和批价" not in page and "第二段继续写机制" not in page
    assert "ocr-reading" not in page and "ocr-link" not in page
    model = append_ocr("", compose_ocr_text([prose]))
    assert "渠道反馈和批价" in model and "第二段继续写机制" in model
    assert model.index("渠道反馈和批价") < model.index("第二段继续写机制")


# 文章 24016：识图把 OK 超市和 Costco 的表读成「一行一条记录，单元格之间一个空格」。
_OCR_TABLE = """OK超市近4年业绩汇总
项目 2020/3月末 2021/3月末 2022/3月末 2023/3月末
年销售额（亿元人民币） 217.6477 254.1157 262.1495 276.3207
毛利率（%） 21.53% 21.86% 21.98% 21.89%
门店数（店） 123 128 134 142
员工人数（人）（兼职工按8小 13038 14873 15572 16284
注：根据OK财报汇总整理"""


def test_ocr_table_stays_on_the_image_and_in_the_model_input():
    """24016：阅读页是业绩截图本身。数字表只出现在交给模型的图片文字里。"""
    digest = "12" * 32
    html = str(render_body("![](ok.png)\n", {"ok.png": f"/media/fangwei-mp/12/{digest}.jpg"}))
    page = inline_image_ocr(html, {digest: _OCR_TABLE})
    assert page == html
    assert f'id="{image_anchor(digest)}"' in page
    assert f'src="/media/fangwei-mp/12/{digest}.jpg"' in page
    assert "ocr-table" not in page and "ocr-link" not in page and "<table" not in page
    assert "217.6477" not in page and "OK超市近4年业绩汇总" not in page
    model = append_ocr("见图。", compose_ocr_text([_OCR_TABLE]))
    assert "【图片里的字】" in model
    assert "OK超市近4年业绩汇总" in model
    assert "217.6477" in model and "21.53%" in model
    assert "注：根据OK财报汇总整理" in model
    assert model.index("OK超市近4年业绩汇总") < model.index("217.6477") < model.index("注：根据OK财报汇总整理")


def test_ocr_markup_is_not_inserted_into_reading_html():
    digest = "34" * 32
    html = str(render_body("![](cost.png)\n", {"cost.png": f"/media/x/34/{digest}.jpg"}))
    raw = "项目 <b>FY2023</b> FY2024\n费用率（SG&A） 9.08% 9.14%"
    page = inline_image_ocr(html, {digest: raw})
    assert page == html
    assert f'<img id="{image_anchor(digest)}"' in page
    assert "<b>" not in page and "FY2023" not in page and "9.08%" not in page
    model = append_ocr("", compose_ocr_text([raw]))
    assert "FY2023" in model and "9.08%" in model and "SG&A" in model


def test_24008_screenshots_stay_images_while_models_read_the_transcript():
    """24008：公众号两张截图在阅读页仍是图，字只进模型输入。没有结构的旧识图照认出来的样子交，不接折行、不排先后。"""
    fang = "ab" * 32
    nest = "cd" * 32
    fang_text = (
        "方洪波：\n企业家精神是敢于在最顺的时候\n对自己动刀子\n光做家电\n没有未来\n"
        "我们光做家电没有未来\nthat just making home appliances offered no future.\n"
        "转型本质就是转人，在企业最顺的时候对自己动刀子 (1/3)\n"
    )
    nest_text = (
        "网友：李录投资也很好，我们投资的基本理念其实非常接近，能力圈可能有点不同而已。"
        "目前我不会考虑和任何人做任何节目的。\n"
        "网友：大道会不会考虑和李录做一期对谈节目？\n"
        "网友：莫尼什：我是苹果用户。段永平拥有OPPO和vivo，也有一个很大的股票投资组合。"
        "他最大的持仓是苹果，直接竞争对手。他是我的朋友。你知道他怎么跟我说的吗？他…\n"
    )
    html = str(
        render_body(
            "![](fang.png)\n\n![](nest.png)\n",
            {
                "fang.png": f"/media/fangwei-mp/96/{fang}.jpg",
                "nest.png": f"/media/fangwei-mp/8b/{nest}.jpg",
            },
        )
    )
    page = inline_image_ocr(html, {fang: fang_text, nest: nest_text})
    assert page == html
    assert f'id="{image_anchor(fang)}"' in page and f'src="/media/fangwei-mp/96/{fang}.jpg"' in page
    assert f'id="{image_anchor(nest)}"' in page and f'src="/media/fangwei-mp/8b/{nest}.jpg"' in page
    assert "ocr-link" not in page and "ocr-reading" not in page
    assert "企业家精神" not in page and "莫尼什" not in page and "李录投资" not in page
    model = append_ocr("见图。", compose_ocr_text([fang_text, nest_text]))
    assert model == append_ocr("见图。", fang_text.strip() + "\n\n" + nest_text.strip())


def test_misplaced_wechat_images_move_beside_the_matching_paragraph():
    """估到引文里的图，按识图挪到对得上的那一段前面。字不进正文，本来就挨着的不动。"""
    shot = "ab" * 32
    chart = "cd" * 32
    shot_src = f"/media/fangwei-mp/ab/{shot}.jpg"
    chart_src = f"/media/fangwei-mp/cd/{chart}.jpg"
    shot_img = f'<img id="{image_anchor(shot)}" src="{shot_src}" alt="">'
    chart_img = f'<img id="{image_anchor(chart)}" src="{chart_src}" alt="">'
    html = (
        f"<p>可乐没有味觉记忆，早上喝一罐也不会腻。</p>"
        f"<p>{shot_img}<br>奶油苏打喝多了会腻。</p>"
        f"<p>{chart_img}</p>"
        "<p>王小明谈到这件事就表态：</p>"
        "<p>我从来不看生成出来的短剧，塑料感太重。</p>"
        "<p>北美票房在 2020 年以前还能到 115 亿美元，最近只剩下 85 亿。</p>"
    )
    shot_ocr = "王小明：我从来不看生成出来的短剧\n塑料感太重"
    chart_ocr = "\n".join(
        [
            "2014 103.61",
            "2015 111.50",
            "2016 113.80",
            "2017 110.80",
            "2018 118.90",
            "2019 113.60",
            "2020 21.10",
            "2024 85.70",
            "2025 86.60",
        ]
    )
    page = inline_image_ocr(html, {shot: shot_ocr, chart: chart_ocr})
    assert page == inline_image_ocr(page, {shot: shot_ocr, chart: chart_ocr})
    assert "ocr-reading" not in page and "103.61" not in page and "王小明：" not in page
    assert "奶油苏打喝多了会腻" in page and "可乐没有味觉记忆" in page
    quote = page.index("我从来不看生成出来的短剧")
    table = page.index("北美票房在 2020 年以前")
    assert page.index(shot_img) < quote
    assert page.index("王小明谈到这件事就表态") < page.index(shot_img) < quote
    assert page.index(chart_img) < table
    assert "85 亿" in page
    # 已经放在对的段前面时，再跑也不改字符串。
    parked = (
        "<p>开场一句。</p>"
        f"{shot_img}"
        "<p>我从来不看生成出来的短剧，塑料感太重。</p>"
    )
    assert inline_image_ocr(parked, {shot: shot_ocr}) == parked


def test_image_relocation_overlap_matches_the_plain_definition():
    """挪图比的是两边汉字最长的一段重合（5 到 24 字）。从共有片段往后接着比，结果要和逐个长度切片比一样。"""

    def plain(left: str, right: str) -> int:
        if len(left) < _PHRASE or len(right) < _PHRASE:
            return 0
        cap = min(24, len(left), len(right))
        for size in range(cap, _PHRASE - 1, -1):
            grams = {left[i : i + size] for i in range(len(left) - size + 1)}
            if any(right[i : i + size] in grams for i in range(len(right) - size + 1)):
                return size
        return 0

    rng = random.Random(7)
    for _ in range(3000):
        alphabet = "甲乙丙丁戊己"[: rng.randint(1, 6)]
        left = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 50)))
        right = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 50)))
        if left and rng.random() < 0.3:
            start = rng.randrange(len(left))
            middle = len(right) // 2
            right = right[:middle] + left[start : start + rng.randint(5, 30)] + right[middle:]
        assert _phrase_overlap(left, right) == plain(left, right), (left, right)
    # 群聊截图里一长串「哈」：只从接不上前一个字的地方往后比，不在同一段重合里反复比。
    assert _phrase_overlap("哈" * 1500, ("哈" * 23 + "嘿") * 10) == 23


def test_image_relocation_on_a_long_article_keeps_its_choices():
    """图多、段多的公众号长文：每张图仍挪到汉字重合最长的那一段前面，对不上的不动。"""
    rng = random.Random(3)
    letters = [chr(code) for code in range(0x4E00, 0x4E00 + 2000)]

    def words(size: int) -> str:
        return "".join(rng.choice(letters) for _ in range(size))

    paragraphs = [words(120) for _ in range(80)]
    hashes = [f"{index:02x}" * 32 for index in range(40)]
    texts = {}
    body = []
    for index, paragraph in enumerate(paragraphs):
        if index < len(hashes):
            image = hashes[index]
            body.append(f'<p><img id="{image_anchor(image)}" src="/media/fangwei-mp/{image[:2]}/{image}.jpg"></p>')
            # 偶数张图讲的是往后 10 段的那一段；奇数张图的字哪段都对不上。
            texts[image] = paragraphs[index + 10][20:40] if index % 2 == 0 else words(300)
        body.append(f"<p>{paragraph}</p>")
    page = inline_image_ocr("".join(body), texts)
    for index, image in enumerate(hashes):
        at = page.index(f"{image}.jpg")
        if index % 2 == 0:
            target = page.index(paragraphs[index + 10])
            assert at < target and not any(at < page.index(p) < target for p in paragraphs), index
        else:
            assert page.index(paragraphs[index - 1]) < at < page.index(paragraphs[index]), index


def test_reader_images_load_lazily_without_touching_other_markup():
    """阅读页的图滚到附近才下。已经写了 loading 的不改，别的标签和图片地址不动，加两遍和加一遍一样。"""
    html = (
        '<p>正文<img id="img-ab" src="/media/a.png" alt="图"></p>'
        '<p><img src="/media/b.png" alt="" /></p>'
        '<p><IMG SRC="/media/c.png" loading="eager"></p><p>imgur 不是图</p>'
    )
    once = lazy_images(html)
    assert once == lazy_images(once)
    assert '<img id="img-ab" src="/media/a.png" alt="图" loading="lazy" decoding="async">' in once
    assert '<img src="/media/b.png" alt="" loading="lazy" decoding="async" />' in once
    assert '<IMG SRC="/media/c.png" loading="eager">' in once
    assert once.count("loading=") == 3 and "<p>imgur 不是图</p>" in once
    assert lazy_images("") == "" and lazy_images("<p>没有图</p>") == "<p>没有图</p>"


def test_only_wechat_bodies_with_estimated_positions_get_their_images_moved():
    """方伟公众号以前的正文是极致了按段估的图位置；从公众号原文页取的（raw_meta.images = page）和别的来源都不挪。"""
    from researchlib.render import images_placed_by_estimate

    assert images_placed_by_estimate("wechat_mp")
    assert images_placed_by_estimate("wechat_mp", {"format": "markdown"})
    assert not images_placed_by_estimate("wechat_mp", {"images": "page"})
    assert not images_placed_by_estimate("feishu")
    assert not images_placed_by_estimate("markdown", {})



def test_highlight_does_not_split_html_entities():
    # 审核 R8：以前先转义再找词，搜 amp 页面显示「&<mark>amp</mark>;」，搜 AT&T 标不上。
    from researchlib.snippets import highlight_full

    assert str(highlight_full("Sales & Marketing", ["amp"])) == "Sales &amp; Marketing"
    assert str(highlight_full("AT&T 和 <b>", ["AT&T", "lt"])) == "<mark>AT&amp;T</mark> 和 &lt;b&gt;"
    assert str(highlight_full("广告加载率", ["广告", "加载"])) == "<mark>广告加载</mark>率"
    assert str(highlight_full("<script>", ["script"])) == "&lt;<mark>script</mark>&gt;"


def test_a_repeated_image_gets_its_anchor_only_once():
    # 审核 F16：同一张图出现两次时 id 重复，#img-… 只能跳到第一处。
    from researchlib.render import _anchor_images

    digest = "a" * 64
    html = f'<p><img src="/media/x/{digest}.png" alt=""></p><p><img src="/media/x/{digest}.png" alt=""></p>'
    out = _anchor_images(html)
    assert out.count(f'id="{image_anchor(digest)}"') == 1
    assert out.count("<img") == 2

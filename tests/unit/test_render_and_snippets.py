from html.parser import HTMLParser

import pytest

from researchlib.render import image_anchor, inline_image_ocr, render_body, sanitize_html, tidy_ocr_text
from researchlib.snippets import answer_html, highlight_snippet
from researchlib.textutil import argumentative_excerpt, markdown_to_text


def test_snippet_keeps_a_late_hit():
    body = ("前言。" * 40) + "这里才提到腾讯。" + ("后记。" * 10)
    rendered = str(highlight_snippet(body, "腾讯"))
    assert rendered.startswith("…")
    assert "<mark>腾讯</mark>" in rendered


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


def test_body_speaker_label_with_a_nickname_is_bold():
    """正文行首的「昵称：」和「方伟：」一样加粗；来源、结论这类元信息词不是说话人。"""
    page = str(render_body("大道无形我有型：一吨就是2000瓶。\n", {}))
    assert '<span class="who">大道无形我有型：</span>一吨就是2000瓶。' in page
    plain = str(render_body("来源：思想×岭南\n\n结论：先看基本面。\n", {}))
    assert 'class="who"' not in plain


def test_chat_screenshot_is_readable_text_and_the_image_is_collapsed():
    """14886 这种：正文一句，群聊在截图里。阅读页直接给出对话，原图要点开。"""
    digest = "ab" * 32
    html = str(
        render_body(
            "瑞幸古茗是比较头部的。\n\n![](chat.png)\n",
            {"chat.png": f"/media/fangwei-zsxq/ab/{digest}.png"},
        )
    )
    page = inline_image_ocr(html, {digest: CHAT})
    assert "瑞幸古茗是比较头部的" in page
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" in page
    assert "古茗店效还行" in page
    anchor = f'<a class="ocr-link" id="{image_anchor(digest)}" href="/media/fangwei-zsxq/ab/{digest}.png"'
    assert anchor in page
    assert "原图 1" not in page
    assert f'<img id="{image_anchor(digest)}"' not in page
    assert "<details" not in page
    linked = page.index(anchor)
    assert linked < page.index("瑞幸和古茗都算头部", linked) < page.index("</a>", linked)
    assert 'class="ocr-reading ocr-dialogue"' in page
    # 截图上的网名当说话人，加粗成 .who；没有绿色气泡（方伟）时也不硬写网友。
    assert '<span class="who">张三：</span>' in page
    assert '<span class="who">李四：</span>' in page
    assert "方伟：" not in page
    assert "<p>微信</p>" not in page and "20:41" not in page
    assert page.count('class="ocr-shot"') == 1


def test_short_or_empty_ocr_keeps_the_image():
    digest = "cd" * 32
    html = str(render_body("![](logo.png)\n", {"logo.png": f"/media/x/cd/{digest}.png"}))
    page = inline_image_ocr(html, {digest: "logo"})
    assert f'<img id="{image_anchor(digest)}"' in page
    assert "点击看原图" not in page
    assert inline_image_ocr(html, {}) == html


def test_ocr_text_is_escaped_and_prose_keeps_newlines():
    digest = "ef" * 32
    html = str(render_body("![](note.png)\n", {"note.png": f"/media/x/ef/{digest}.png"}))
    prose = (
        "<script>alert(1)</script>这是贴成图片的一段长文，讲渠道反馈和批价。\n"
        "第二段继续写机制，不是一行一句的聊天记录。"
    )
    page = inline_image_ocr(html, {digest: prose})
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "第二段继续写机制" in page
    assert 'class="ocr-reading"' in page and "ocr-chat" not in page
    assert "渠道反馈和批价。" in page
    assert page.index("渠道反馈和批价。") < page.index("第二段继续写机制")
    assert f'<a class="ocr-link" id="{image_anchor(digest)}"' in page


# 文章 24016：识图把 OK 超市和 Costco 的表读成「一行一条记录，单元格之间一个空格」。
_OCR_TABLE = """OK超市近4年业绩汇总
项目 2020/3月末 2021/3月末 2022/3月末 2023/3月末
年销售额（亿元人民币） 217.6477 254.1157 262.1495 276.3207
毛利率（%） 21.53% 21.86% 21.98% 21.89%
门店数（店） 123 128 134 142
员工人数（人）（兼职工按8小 13038 14873 15572 16284
注：根据OK财报汇总整理"""


def test_ocr_table_renders_as_a_real_table():
    """24016：OK 超市、Costco 的业绩表要是 <table>，不能一行一段排下去。"""
    digest = "12" * 32
    html = str(render_body("![](ok.png)\n", {"ok.png": f"/media/fangwei-mp/12/{digest}.jpg"}))
    page = inline_image_ocr(html, {digest: _OCR_TABLE})
    assert '<table class="ocr-table">' in page
    assert '<thead><tr><th scope="col">项目</th>' in page
    assert '<th scope="col">2020/3月末</th>' in page
    assert (
        '<tr><th scope="row">年销售额（亿元人民币）</th>'
        "<td>217.6477</td><td>254.1157</td><td>262.1495</td><td>276.3207</td></tr>"
    ) in page
    assert '<tr><th scope="row">毛利率（%）</th><td>21.53%</td>' in page
    # 表头和每一行都不再各占一个 <p>。
    assert "<p>项目 2020/3月末" not in page
    assert "<p>年销售额（亿元人民币）" not in page
    assert "<p>门店数（店） 123" not in page
    # 标题、注记在表外，仍是段。
    assert ">OK超市近4年业绩汇总</a></p>" in page
    assert ">注：根据OK财报汇总整理</a></p>" in page
    assert page.index("OK超市近4年业绩汇总") < page.index("<table") < page.index("注：根据OK财报汇总整理")
    # 原图链接和 figure 壳保住；锚点落在第一个块（标题）上。
    assert 'class="ocr-shot"' in page
    anchor = f'<a class="ocr-link" id="{image_anchor(digest)}" href="/media/fangwei-mp/12/{digest}.jpg"'
    assert anchor in page
    assert 'class="ocr-link ocr-table-link"' in page
    assert f'href="/media/fangwei-mp/12/{digest}.jpg"' in page[page.index("<table") - 400 : page.index("<table")]


def test_ocr_table_cells_are_escaped():
    digest = "34" * 32
    html = str(render_body("![](cost.png)\n", {"cost.png": f"/media/x/34/{digest}.jpg"}))
    page = inline_image_ocr(html, {digest: "项目 <b>FY2023</b> FY2024\n费用率（SG&A） 9.08% 9.14%"})
    assert "<table" in page
    assert "&lt;b&gt;FY2023&lt;/b&gt;" in page
    assert "SG&amp;A" in page
    assert "<b>" not in page


def test_chrome_cleanup_does_not_eat_the_message():
    cleaned = tidy_ocr_text(CHAT)
    assert "微信" not in cleaned.splitlines()
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" in cleaned
    assert tidy_ocr_text("微信\n20:41\n昨天") == "微信\n20:41\n昨天"


def test_24008_quote_paragraphs_and_xueqiu_order_are_in_text_links():
    """24008：方洪波收成自然段，雪球较早的发言在前，字本身链到原图。"""
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
    page = inline_image_ocr(
        str(
            render_body(
                "![](fang.png)\n\n![](nest.png)\n",
                {
                    "fang.png": f"/media/fangwei-mp/96/{fang}.jpg",
                    "nest.png": f"/media/fangwei-mp/8b/{nest}.jpg",
                },
            )
        ),
        {fang: fang_text, nest: nest_text},
    )
    assert "企业家精神是敢于在最顺的时候对自己动刀子" in page
    assert "时候</p>" not in page and "对自己动刀子</p>" not in page.split("企业家精神", 1)[-1][:80]
    assert "光做家电</a>" in page and "没有未来</a>" in page
    assert "刀子光做家电" not in page
    assert "future.</a>" in page and "转型本质就是转人" in page
    spirit = page.index("企业家精神是敢于在最顺的时候对自己动刀子")
    spirit_end = page.index("</p>", spirit)
    assert "转型本质" not in page[spirit:spirit_end]
    assert page.index("莫尼什：") < page.index("大道会不会考虑") < page.index("李录投资也很好")
    assert "原图 1" not in page and "原图 2" not in page
    fang_at = page.index(f'id="{image_anchor(fang)}"')
    assert fang_at < page.index("企业家精神是敢于在最顺的时候对自己动刀子", fang_at)
    assert f'href="/media/fangwei-mp/96/{fang}.jpg"' in page[fang_at : page.index("</a>", fang_at)]
    nest_at = page.index(f'id="{image_anchor(nest)}"')
    assert nest_at < page.index("莫尼什：", nest_at) < page.index("</a>", nest_at)
    assert f'href="/media/fangwei-mp/8b/{nest}.jpg"' in page[nest_at : page.index("</a>", nest_at)]
    reply_at = page.index("李录投资也很好")
    reply_open = page.rindex("<a ", 0, reply_at)
    assert f"/media/fangwei-mp/8b/{nest}.jpg" in page[reply_open : page.index("</a>", reply_at)]

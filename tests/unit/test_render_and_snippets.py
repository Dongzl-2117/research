from html.parser import HTMLParser

import pytest

from researchlib.render import image_anchor, inline_image_ocr, render_body, sanitize_html, tidy_ocr_text
from researchlib.snippets import highlight_snippet, literal_match_label
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


def test_match_label_counts_visible_theme_in_each_window():
    groups = [["维护"], ["合同"], ["赔偿"]]
    partial = highlight_snippet("维护合同在到期后可以续签。", "维护 合同 赔偿")
    assert literal_match_label([partial], groups, company=True) == "部分词匹配，主题未核实"
    context = highlight_snippet("上段列出了赔偿条件。", "赔偿")
    assert literal_match_label([context, partial], groups, company=True) == "字面匹配"
    unrelated = highlight_snippet("本段讲每年的收入。", "维护 合同 赔偿")
    assert literal_match_label([unrelated], groups, company=True) == "同公司参考"


def test_match_label_does_not_credit_cropped_text_or_english_substrings():
    body = "维护合同条款。" + "这里是其他说明。" * 40 + "最后才讨论赔偿。"
    visible = highlight_snippet(body, "维护 合同 赔偿", radius=48)
    assert "赔偿" not in visible
    assert literal_match_label([visible], [["维护"], ["合同"], ["赔偿"]]) == "部分词匹配，主题未核实"
    substring = highlight_snippet("Auditing systems", "audit")
    assert literal_match_label([substring], [["audit"]], company=True) == "同公司参考"


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


def test_callout_renders_as_a_plain_quote():
    html = str(render_body(CALLOUT, {}))
    assert "[!" not in html and "NOTE" not in html
    assert "<blockquote>" in html
    assert "石油企业的运行九成是政治，一成是石油。" in html
    assert "能力圈有多大不要紧！" in html


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
    assert "原图 1" in page
    assert f'<a class="ocr-link" id="{image_anchor(digest)}" href="/media/fangwei-zsxq/ab/{digest}.png"' in page
    assert f'<img id="{image_anchor(digest)}"' not in page
    assert "<details" not in page
    assert page.index("瑞幸和古茗都算头部") < page.index("原图 1")
    assert 'class="ocr-reading ocr-dialogue"' in page
    assert "网友：" in page
    assert "张三" not in page and "李四" not in page
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
    assert "渠道反馈和批价。\n第二段" in page


def test_chrome_cleanup_does_not_eat_the_message():
    cleaned = tidy_ocr_text(CHAT)
    assert "微信" not in cleaned.splitlines()
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" in cleaned
    assert tidy_ocr_text("微信\n20:41\n昨天") == "微信\n20:41\n昨天"

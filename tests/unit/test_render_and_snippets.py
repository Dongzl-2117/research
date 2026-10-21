from researchlib.render import image_anchor, inline_image_ocr, render_body, sanitize_html, tidy_ocr_text
from researchlib.snippets import highlight_snippet


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


def test_sanitize_javascript_url():
    cleaned = sanitize_html('<a href="javascript:alert(1)">链接</a>')
    assert "javascript:" not in cleaned


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
    assert "点击看原图" in page
    assert f'<figure class="ocr-shot" id="{image_anchor(digest)}">' in page
    assert f'<img id="{image_anchor(digest)}"' not in page
    assert page.index("瑞幸和古茗都算头部") < page.index("<details")
    assert 'class="ocr-reading ocr-dialogue"' in page
    assert "网友：" in page
    assert "张三" not in page and "李四" not in page
    assert "方伟：" not in page
    assert "<p>微信</p>" not in page and "20:41" not in page
    assert f'<img src="/media/fangwei-zsxq/ab/{digest}.png"' in page


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

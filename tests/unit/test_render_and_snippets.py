from researchlib.render import render_body, sanitize_html
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

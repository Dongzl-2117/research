"""回答展示不能破坏代码、引用定位或把 Markdown 链接变成可执行内容。"""

from html.parser import HTMLParser

import pytest

from researchlib.snippets import answer_html


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def test_markdown_preserves_structure_and_only_links_known_prose_citations():
    text = ("## 结论\n\n**重点**[1000]，未提供的[9]。\n\n"
            "1. 第一项\n2. 第二项\n\n> 保留引文\n\n"
            "| 项目 | 数字 |\n| --- | --- |\n| 收入 | 10 |\n\n"
            "```python\nif a < b: print('[1000]')\n```\n\n"
            "行内 `[1000]`，普通链接 [资料[1000]](https://example.invalid/a[1000])。")
    html = str(answer_html(text, [{"n": 1000}]))
    for tag in ("h2", "strong", "ol", "blockquote", "table", "pre", "code"):
        assert f"<{tag}" in html
    assert "if a &lt; b" in html and "&amp;lt;" not in html
    links = Links()
    links.feed(html)
    assert [item["href"] for item in links.links if item.get("class") == "cite"] == ["#c-1000"]
    assert "https://example.invalid/a[1000]" in html


@pytest.mark.parametrize("link", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x",
                                  "vbscript:msgbox(1)", "java&#x73;cript:alert(1)"])
def test_markdown_rejects_executable_link_schemes(link):
    html = str(answer_html(f"[链接]({link})"))
    parsed = Links()
    parsed.feed(html)
    assert all("href" not in item for item in parsed.links)


def test_raw_html_is_visible_text_and_external_images_never_load():
    text = '<script>alert(1)</script>\n\n<img src="/media/a" onerror="alert(1)">\n\n![外图](https://example.invalid/x)'
    html = str(answer_html(text))
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_citation_patterns_inside_attributes_and_code_are_preserved():
    html = str(answer_html('[资料](https://example.invalid/[1] "标题[1]") `[1]` [1]', [{"n": 1}]))
    parsed = Links()
    parsed.feed(html)
    assert parsed.links == [{"href": "https://example.invalid/[1]", "title": "标题[1]"},
                            {"class": "cite", "href": "#c-1"}]
    assert '<code>[1]</code>' in html

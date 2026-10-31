"""文章 1297 这类飞书正文：相对 .md 链接不能留在渲染结果里。"""

import re

from researchlib.mdlinks import MdIndex, prepare_body_html, relative_md_hrefs

# 生产上的 1297 是飞书导出，正文里有上百个 `标题__token.md`。这里用同样的形状做回归。
ARTICLE_1297_LINKS = "\n".join(
    [
        "# 零售研究",
        "",
        "见 [零售研究](零售研究__JsoOabc1.md)。",
        "",
        "改过名的仍应连上 [旧标题](旧标题__Tokn1234.md)。",
        "",
        "[外链笔记](https://example.com/files/note.md)",
        "",
        "[这段](/articles/1?chunk=3#p-2)",
        "",
        *[f"- [没有的文档{i}](没有的文档{i}__Nope{i:04d}.md)" for i in range(80)],
        *[f"- [文档{i}](文档{i}__Tok{i:04d}.md)" for i in range(40)],
    ]
)


def _index():
    rows = [
        {"id": 1297, "external_id": "docs/零售研究__JsoOabc1.md"},
        {"id": 1400, "external_id": "docs/新标题__Tokn1234.md"},
        *({"id": 2000 + i, "external_id": f"docs/文档{i}__Tok{i:04d}.md"} for i in range(40)),
        # 同一个 token 出现两次时不能猜。
        {"id": 3001, "external_id": "docs/甲__DupToken99.md"},
        {"id": 3002, "external_id": "docs/乙__DupToken99.md"},
    ]
    return MdIndex(rows)


def test_article_1297_html_has_no_relative_md_hrefs():
    html = prepare_body_html(ARTICLE_1297_LINKS, "零售研究", {}, None, _index(), "docs")
    assert relative_md_hrefs(html) == []
    assert 'href="/articles/1297"' in html
    assert 'href="/articles/1400"' in html
    assert 'href="https://example.com/files/note.md"' in html
    assert 'href="/articles/1?chunk=3#p-2"' in html
    assert html.count("未收录") == 80
    assert "没有的文档0" in html
    assert 'href="/articles/2000"' in html
    assert re.search(r'id="p-\d+"', html)


def test_ambiguous_token_is_not_linked():
    body = "见 [甲或乙](随便__DupToken99.md)。"
    html = prepare_body_html(body, "笔记", {}, None, _index(), "docs")
    assert relative_md_hrefs(html) == []
    assert "未收录" in html
    assert 'href="/articles/3001"' not in html
    assert 'href="/articles/3002"' not in html


def test_stratechery_links_to_other_posts_reach_them():
    """Stratechery 的 external_id 是 frontmatter 里的「2015/aggregation-theory」，不带 .md；
    正文里指向别篇的链接是相对这一篇所在目录写的「../2015/aggregation-theory.md」。以前一律「未收录」。"""
    index = MdIndex(
        [
            {"id": 21892, "external_id": "2015/aggregation-theory"},
            {"id": 23038, "external_id": "2016/tv-advertising"},
            {"id": 23041, "external_id": "2016/same-year-post"},
        ]
    )
    body = (
        "Aka [Aggregation Theory](../2015/aggregation-theory.md) [↩](#rf4-2191)\n\n"
        "See [last month](same-year-post.md#part-2) and [an older one](../2014/not-archived.md)."
    )
    html = prepare_body_html(body, "TV Advertising", {}, None, index, "2016")
    assert relative_md_hrefs(html) == []
    assert 'href="/articles/21892"' in html
    assert 'href="/articles/23041"' in html
    assert 'href="#rf4-2191"' in html
    # 库里没有的那篇照旧留文字、标「未收录」。
    assert html.count("未收录") == 1 and "an older one" in html
    # 这一篇的目录没记下来（旧导入）也能按路径找到。
    assert 'href="/articles/21892"' in prepare_body_html(body, "TV Advertising", {}, None, index, "")

"""知识星球评论区只留方伟。"""

from researchlib.render import render_body
from researchlib.zsxq_comments import (
    assemble_reader_comments,
    comment_list_authors,
    filter_host_comments_markdown,
    guest_comment_authors,
    is_host_comment_author,
)

SAMPLE = """正文一句。

## 评论（3）

- **曾广文**（2026-10-21 11:42）
  个人觉得大模型很难形成护城河。
- **方伟Rey**（2026-10-21 11:50）
  护城河还是要看切换成本。
- **　　　　　　　**（2026-10-21 11:51）
  空白名字不是方伟。

## 评论里的回复

作者答：后面另说。
"""


def test_host_author_detection():
    assert is_host_comment_author("方伟")
    assert is_host_comment_author("**方伟Rey**")
    assert is_host_comment_author("方伟-最近在杭州")
    assert not is_host_comment_author("曾广文")
    assert not is_host_comment_author("　　　　　　　")
    assert not is_host_comment_author("")


def test_filter_markdown_does_not_trust_even_fangwei_nickname():
    filtered = filter_host_comments_markdown(SAMPLE)
    assert "正文一句。" in filtered
    assert "方伟Rey" not in filtered
    assert "护城河还是要看切换成本" not in filtered
    assert "曾广文" not in filtered
    assert "空白名字" not in filtered
    assert "## 评论里的回复" not in filtered
    assert "作者答：后面另说" not in filtered


def test_filter_markdown_drops_section_without_host():
    md = """正文。

## 评论（1）

- **曾广文**（2026-10-21 11:42）
  只有网友。
"""
    filtered = filter_host_comments_markdown(md)
    assert "## 评论" not in filtered
    assert "曾广文" not in filtered
    assert "正文。" in filtered


def test_guest_roster_lists_only_non_host_and_ignores_reply_section():
    assert guest_comment_authors(SAMPLE) == ["曾广文", "匿名"]
    assert comment_list_authors(SAMPLE) == ["曾广文", "方伟Rey", "匿名"]
    assert guest_comment_authors("正文。\n\n## 评论里的回复\n\n读者问：问。\n") == []
    assert comment_list_authors("正文。\n\n## 评论（1）\n\n这里不是列表。\n") is None


def test_reading_page_hides_guest_comments():
    html = assemble_reader_comments(SAMPLE, {}, lambda body, mapping: f"<p>{body.strip()}</p>")
    assert "正文一句。" in html
    assert "zsxq-comments" not in html
    assert "方伟Rey" not in html
    assert "护城河还是要看切换成本" not in html
    assert "曾广文" not in html
    assert "空白名字" not in html
    assert "评论（1）" not in html


def test_render_body_on_article_with_comments():
    page = str(render_body(SAMPLE, {}))
    assert "正文一句。" in page
    assert "方伟Rey" not in page
    assert "曾广文" not in page
    assert 'class="zsxq-comments"' not in page

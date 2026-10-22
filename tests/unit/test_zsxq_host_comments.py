"""知识星球评论区只留方伟。"""

from researchlib.render import render_body
from researchlib.zsxq_comments import (
    assemble_reader_comments,
    filter_host_comments_markdown,
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


def test_filter_markdown_keeps_only_fangwei():
    filtered = filter_host_comments_markdown(SAMPLE)
    assert "## 评论（1）" in filtered
    assert "方伟Rey" in filtered
    assert "护城河还是要看切换成本" in filtered
    assert "曾广文" not in filtered
    assert "空白名字" not in filtered
    assert "## 评论里的回复" in filtered
    assert "作者答：后面另说" in filtered


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


def test_reading_page_hides_guest_comments():
    html = assemble_reader_comments(SAMPLE, {}, lambda body, mapping: f"<p>{body.strip()}</p>")
    assert "zsxq-comments" in html
    assert "方伟Rey" in html
    assert "护城河还是要看切换成本" in html
    assert "曾广文" not in html
    assert "空白名字" not in html
    assert "评论（1）" in html


def test_render_body_on_article_with_comments():
    page = str(render_body(SAMPLE, {}))
    assert "方伟Rey" in page
    assert "曾广文" not in page
    assert 'class="zsxq-comments"' in page

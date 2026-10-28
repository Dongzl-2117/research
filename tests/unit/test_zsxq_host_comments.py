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

- **陈小东**（2026-10-21 11:42）
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
    assert not is_host_comment_author("陈小东")
    assert not is_host_comment_author("　　　　　　　")
    assert not is_host_comment_author("")


def test_filter_markdown_keeps_only_fangwei():
    filtered = filter_host_comments_markdown(SAMPLE)
    assert "## 评论（1）" in filtered
    assert "方伟Rey" in filtered
    assert "护城河还是要看切换成本" in filtered
    assert "陈小东" not in filtered
    assert "空白名字" not in filtered
    assert "## 评论里的回复" in filtered
    assert "作者答：后面另说" in filtered


def test_filter_markdown_drops_section_without_host():
    md = """正文。

## 评论（1）

- **陈小东**（2026-10-21 11:42）
  只有网友。
"""
    filtered = filter_host_comments_markdown(md)
    assert "## 评论" not in filtered
    assert "陈小东" not in filtered
    assert "正文。" in filtered


def test_guest_roster_lists_only_non_host_and_ignores_reply_section():
    assert guest_comment_authors(SAMPLE) == ["陈小东", "匿名"]
    assert comment_list_authors(SAMPLE) == ["陈小东", "方伟Rey", "匿名"]
    assert guest_comment_authors("正文。\n\n## 评论里的回复\n\n读者问：问。\n") == []
    assert comment_list_authors("正文。\n\n## 评论（1）\n\n这里不是列表。\n") is None


def test_reading_page_hides_guest_comments():
    html = assemble_reader_comments(SAMPLE, {}, lambda body, mapping: f"<p>{body.strip()}</p>")
    assert "zsxq-comments" in html
    assert "方伟Rey" in html
    assert "护城河还是要看切换成本" in html
    assert "陈小东" not in html
    assert "空白名字" not in html
    assert "评论（1）" in html


def test_render_body_on_article_with_comments():
    page = str(render_body(SAMPLE, {}))
    assert "方伟Rey" in page
    assert "陈小东" not in page
    assert 'class="zsxq-comments"' in page


OTHER_STAR = """另一个星球的正文。

## 评论（3）

- **乘风破浪（作者）**（2026-10-21 11:42）
  补充一句：关税对半托管影响有限。
- **王小明**（2026-10-21 11:50）
  同意。
- **方伟Rey**（2026-10-21 11:51）
  不是这个星球的星主。
"""


def test_other_star_keeps_its_own_author_comments():
    """乘风破浪的星球：留乘风破浪自己的评论，别人的（包括方伟）不留。不知道来源时照旧只认方伟。"""
    from researchlib.zsxq_comments import source_owners

    class Source:
        author = "乘风破浪"
        poster_names = ()

    owners = source_owners(Source())
    assert owners == ("乘风破浪",)
    assert is_host_comment_author("乘风破浪（作者）", owners)
    assert not is_host_comment_author("王小明", owners)
    assert not is_host_comment_author("方伟Rey", owners)
    assert is_host_comment_author("方伟Rey", ("方伟", "方伟Rey"))
    filtered = filter_host_comments_markdown(OTHER_STAR, owners)
    assert "## 评论（1）" in filtered and "关税对半托管影响有限" in filtered
    assert "王小明" not in filtered and "不是这个星球的星主" not in filtered
    assert guest_comment_authors(OTHER_STAR, owners) == ["王小明", "方伟Rey"]
    page = str(render_body(OTHER_STAR, {}, owners=owners))
    assert "关税对半托管影响有限" in page and "王小明" not in page
    # 不给来源时和以前一样：只留方伟。
    legacy = str(render_body(OTHER_STAR, {}))
    assert "不是这个星球的星主" in legacy and "关税对半托管影响有限" not in legacy


def test_zsxq_import_keeps_the_source_author_comments(tmp_path):
    from researchlib.ingest.parse import parse_archive

    (tmp_path / "md").mkdir()
    (tmp_path / "md" / "777777.md").write_text(OTHER_STAR, encoding="utf-8")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq", owners=("乘风破浪",))
    assert errors == []
    assert "关税对半托管影响有限" in docs[0].body_md and "王小明" not in docs[0].body_md

"""阅读页展示层：对话标签统一、明显错字。"""

from researchlib.present_text import (
    normalize_dialogue_labels,
    polish_display_text,
    present_article_text,
)
from researchlib.render import render_body


def test_legacy_comment_labels_become_fangwei_wangyou():
    raw = "## 评论里的回复\n\n读者问：奈飞怎么样？\n\n作者答：运营杠杆还在。\n\n作者补充：再看一眼广告。\n"
    out = normalize_dialogue_labels(raw)
    assert "网友：奈飞怎么样？" in out
    assert "方伟：运营杠杆还在。" in out
    assert "方伟：再看一眼广告。" in out
    assert "读者问" not in out and "作者答" not in out and "作者补充" not in out


def test_polish_fixes_obvious_typos_only():
    assert polish_display_text("AI算力是一个很新都东西") == "AI算力是一个很新的东西"
    assert polish_display_text("存在运用杠杆的好处") == "存在运营杠杆的好处"
    # 口语不动
    assert polish_display_text("我觉得还行吧") == "我觉得还行吧"


def test_youtube_netflix_zsxq_fixture_reads_as_one_dialogue_skin():
    """截图像 23999：正文短句 + 图（此处省略）+ 评论里的回复旧标签。"""
    body = """YouTube的护城河类型是平台网络效应，Netflix只是规模效应，前者比后者强

## 评论里的回复

读者问：奈飞生意其实很不错的，每年内容成本固定200亿。

作者答：这里面很多人误会的应该是觉得YouTube的成本比Netflix低，其实不然，YouTube是可变成本低，它是一半要分给创作者，Netflix是存在运用杠杆的好处。
"""
    html = str(render_body(body, {}))
    assert "读者问" not in html and "作者答" not in html
    assert '<span class="who">网友：</span>' in html
    assert '<span class="who">方伟：</span>' in html
    assert "存在运营杠杆的好处" in html
    assert "评论里的回复" in html
    # 评论区不是另一套 zsxq-comment 列表，而是与正文同一段落对话体
    assert "zsxq-comment-author" not in html


def test_present_article_text_composes_label_and_typo():
    text = "读者问：很新都东西\n\n作者答：存在运用杠杆"
    assert present_article_text(text) == "网友：很新的东西\n\n方伟：存在运营杠杆"

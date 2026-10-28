"""网站隐藏：网友帖与「图片」评论壳标题。"""

from researchlib.author_voice import (
    hide_from_site,
    is_comment_shell_title,
    is_guest,
    not_comment_shell_sql,
    site_visible_sql,
)


def test_comment_shell_title_patterns():
    assert is_comment_shell_title("「图片」 评论（5） - 某读者")
    assert is_comment_shell_title("「图片」评论（12）-Foo")
    assert is_comment_shell_title("「图片」  评论（1）")
    assert not is_comment_shell_title("「图片」")
    assert not is_comment_shell_title("评论（5） - 某读者")
    assert not is_comment_shell_title("泡泡玛特：")


def test_hide_from_site_guest_or_shell():
    assert hide_from_site({"poster_role": "guest"}, "随便")
    assert hide_from_site({"poster_role": "owner"}, "「图片」 评论（3） - 读者甲")
    assert not hide_from_site({"poster_role": "owner"}, "星主正经帖")
    assert not is_guest({"poster_role": "owner"})


def test_site_visible_sql_shape():
    sql = site_visible_sql("a")
    assert "poster_role" in sql and "guest" in sql
    assert "「图片」" in sql and "评论（" in sql
    bare = site_visible_sql("")
    assert "coalesce(raw_meta" in bare
    assert "title !~" in not_comment_shell_sql("")
    assert "「图片」" in not_comment_shell_sql("")

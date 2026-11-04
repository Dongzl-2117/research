"""知识星球评论区只留来源作者：方伟星球留方伟，别的星球留它自己的星主。"""

import json
from pathlib import Path
from types import SimpleNamespace

from researchlib.ingest.parse import parse_archive
from researchlib.render import render_body
from researchlib.zsxq_comments import (
    assemble_reader_comments,
    comment_list_authors,
    filter_host_comments_markdown,
    guest_comment_authors,
    is_host_comment_author,
    source_owners,
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


# 别的星球（乘风破浪这种）：来源作者名加星主的星球昵称。昵称是编的。
OWNERS = source_owners(SimpleNamespace(author="乘风破浪", poster_names=("赵六",)))
OTHER = """正文一句，讲关税。

## 评论（3）

- **李四**（2026-09-27 15:00）
  半托管早就铺开了吗？
- **赵六**（2026-09-27 15:10）
  铺开了，这次关税是利好。
- **方伟Rey**（2026-09-27 15:20）
  别的星球里的方伟也只是读者。
"""


def test_other_source_keeps_its_own_host():
    assert OWNERS == ("乘风破浪", "赵六")
    assert is_host_comment_author("赵六（作者）", OWNERS)
    assert is_host_comment_author("乘风破浪", OWNERS)
    assert not is_host_comment_author("方伟Rey", OWNERS)
    assert not is_host_comment_author("李四", OWNERS)
    assert guest_comment_authors(OTHER, OWNERS) == ["李四", "方伟Rey"]

    filtered = filter_host_comments_markdown(OTHER, OWNERS)
    assert "## 评论（1）" in filtered and "铺开了，这次关税是利好" in filtered
    assert "李四" not in filtered and "方伟Rey" not in filtered

    page = str(render_body(OTHER, {}, owners=OWNERS))
    assert "铺开了，这次关税是利好" in page
    assert "李四" not in page and "方伟Rey" not in page
    # 不给作者名时和以前一样只认方伟。
    assert "赵六" not in str(render_body(OTHER, {}))
    # 方伟星球给了作者名，「方伟-最近在杭州」这些显示名照样认。
    assert is_host_comment_author("方伟-最近在杭州", ("方伟", "方伟Rey"))


def test_zsxq_import_keeps_the_sources_own_host(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "md" / "888888.md").write_text(OTHER, encoding="utf-8")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq", owners=OWNERS)
    assert errors == []
    assert "铺开了，这次关税是利好" in docs[0].body_md
    assert "李四" not in docs[0].body_md and "方伟Rey" not in docs[0].body_md


# 楼中楼：导出写成「赵六 回复 王小明」。星主回复读者的要留，读者的昵称不留；读者回复星主的不留。
THREAD = """正文一句，讲关税。

## 评论（5）

- **王小明**（2026-09-27 15:00）
  半托管早就铺开了吗？
  - **赵六 回复 王小明**（2026-09-27 15:05）
    三季度才放量。
  - **李 四 回复 赵六**（2026-09-27 15:06）
    读者回星主的话。
  - **赵六 回复 乘风破浪**（2026-09-27 15:07）
    接着自己上一条说。
  - **赵六 回复 　　**（2026-09-27 15:08）
    回空白名字的读者。
"""


def test_host_replies_in_threads_are_kept_without_reader_names():
    assert is_host_comment_author("赵六 回复 王小明", OWNERS)
    assert not is_host_comment_author("李 四 回复 赵六", OWNERS)
    assert not is_host_comment_author("王小明 回复 赵六", OWNERS)
    assert guest_comment_authors(THREAD, OWNERS) == ["王小明", "王小明", "李 四 回复 赵六", "匿名"]

    filtered = filter_host_comments_markdown(THREAD, OWNERS)
    assert "## 评论（3）" in filtered
    assert filtered.count("- **赵六 回复网友**") == 2 and "- **赵六 回复 乘风破浪**" in filtered
    assert "三季度才放量" in filtered and "接着自己上一条说" in filtered and "回空白名字的读者" in filtered
    assert "王小明" not in filtered and "李 四" not in filtered
    assert "半托管早就铺开了吗" not in filtered and "读者回星主的话" not in filtered
    # 清过的再看一遍：没有别人的名字了，接口和回写不会再动它。
    assert guest_comment_authors(filtered, OWNERS) == []

    page = str(render_body(THREAD, {}, owners=OWNERS))
    assert "三季度才放量" in page and "赵六 回复网友" in page
    assert "王小明" not in page and "读者回星主的话" not in page

    # 导入时有话题 JSON：回复连同读者那句进「评论里的回复」，列表里不留。
    assert "三季度才放量" not in filter_host_comments_markdown(THREAD, OWNERS, replies=False)


def test_fangwei_replies_keep_his_name_without_the_reader():
    md = (
        "正文。\n\n## 评论（2）\n\n- **王小明**（2026-10-21 11:42）\n  问。\n"
        "  - **方伟-最近在杭州 回复 王小明**（2026-10-21 11:50）\n    切换成本还在。\n"
    )
    filtered = filter_host_comments_markdown(md)
    assert "- **方伟-最近在杭州 回复网友**（2026-10-21 11:50）" in filtered and "切换成本还在" in filtered
    assert "王小明" not in filtered
    assert guest_comment_authors(md) == ["王小明", "王小明"]


def test_zsxq_import_keeps_host_replies_unless_the_topic_json_has_them(tmp_path: Path):
    md = (
        "拼多多的二季度。\n\n## 评论（2）\n\n- **王小明**（2025-03-01 10:00）\n  Temu 的关税影响有多大？\n"
        "  - **方伟Rey 回复 王小明**（2025-03-01 10:05）\n    半托管的毛利会被吃掉一半。\n"
    )
    (tmp_path / "md").mkdir()
    (tmp_path / "md" / "777777.md").write_text(md, encoding="utf-8")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    body = docs[0].body_md
    assert "- **方伟Rey 回复网友**" in body and "半托管的毛利会被吃掉一半" in body
    assert "王小明" not in body and "Temu 的关税影响有多大" not in body

    owner, reader = {"user_id": 1001, "name": "方伟Rey"}, {"user_id": 2002, "name": "王小明"}
    topic = {
        "topic_id": 777777,
        "create_time": "2025-03-01T09:00:00.000+0800",
        "type": "talk",
        "talk": {"owner": owner, "text": "拼多多的二季度。"},
        "show_comments": [
            {"comment_id": 1, "owner": reader, "text": "Temu 的关税影响有多大？"},
            {"comment_id": 2, "owner": owner, "repliee": reader, "text": "半托管的毛利会被吃掉一半。"},
        ],
    }
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "777777.json").write_text(json.dumps(topic, ensure_ascii=False), encoding="utf-8")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work2", id_mode="zsxq")
    assert errors == []
    body = docs[0].body_md
    assert "## 评论（" not in body
    assert "网友：Temu 的关税影响有多大？" in body and "方伟：半托管的毛利会被吃掉一半。" in body
    assert body.count("半托管的毛利会被吃掉一半") == 1 and "王小明" not in body

"""知识星球评论里作者本人的回复：接在正文后面，连同被回复的那条读者评论；正文里已有的不重复。"""

import json
from pathlib import Path

from researchlib.ingest.parse import author_replies, comments_section, parse_archive, zsxq_text
from researchlib.revisions import split_comments

OWNER = {"user_id": 1001, "name": "方伟"}
READER = {"user_id": 2002, "name": "某读者"}


def _topic(**extra) -> dict:
    topic = {
        "topic_id": 777777,
        "create_time": "2025-03-01T09:00:00.000+0800",
        "type": "talk",
        "talk": {"owner": OWNER, "text": "拼多多的二季度。"},
        "show_comments": [
            {"comment_id": 1, "owner": READER, "text": "Temu 的关税影响有多大？"},
            {"comment_id": 2, "owner": OWNER, "repliee": READER,
             "text": '<e type="mention" uid="2002" title="%40%E6%9F%90%E8%AF%BB%E8%80%85" />'
                     " 半托管的毛利会被吃掉一半。"},
            {"comment_id": 3, "owner": READER, "text": "谢谢"},
            {"comment_id": 4, "owner": OWNER,
             "text": '再补一句：看 <e type="hashtag" hid="1" title="%23%E6%8B%BC%E5%A4%9A%E5%A4%9A%23" />'
                     " 主站的货币化率。"},
        ],
    }
    topic.update(extra)
    return topic


def test_zsxq_markup_is_cleaned_without_reader_names():
    assert zsxq_text('<e type="mention" uid="1" title="%40%E5%BC%A0%E4%B8%89" /> 你好') == "你好"
    assert zsxq_text('看 <e type="web" href="x" title="%E8%B4%A2%E6%8A%A5" /> &amp; 估值') == "看 财报 & 估值"


def test_only_the_authors_replies_are_kept_with_the_question():
    pairs = author_replies(_topic())
    assert pairs == [
        ("Temu 的关税影响有多大？", "半托管的毛利会被吃掉一半。"),
        ("", "再补一句：看 #拼多多# 主站的货币化率。"),
    ]
    # 问答话题：回答的人（星主）就是作者。
    qa = _topic(type="q&a", talk=None, answer={"owner": OWNER, "text": "答"}, question={"owner": READER})
    assert len(author_replies(qa)) == 2
    # 楼中楼摊平。
    nested = _topic(show_comments=[], comments=[
        {"comment_id": 9, "owner": READER, "text": "问题",
         "replied_comments": [{"comment_id": 10, "owner": OWNER, "repliee": READER, "text": "回复"}]},
    ])
    assert author_replies(nested) == [("问题", "回复")]


def test_comments_section_skips_replies_already_in_the_body():
    pairs = [("问一", "答一，主站的货币化率还有空间。"), ("", "补充：关税的影响要分两段看。")]
    section = comments_section(pairs, "正文。")
    assert section.startswith("\n\n## 评论里的回复\n\n")
    assert "读者问：问一" in section and "作者答：答一" in section and "作者补充：补充" in section
    assert "答一" not in comments_section(pairs, "正文里已经有：答一，主站的货币化率还有空间。")
    assert comments_section([], "正文") == ""


def test_split_comments_separates_the_appended_section():
    body = "正文。" + comments_section([("问一", "答一，主站的货币化率还有空间。")], "正文。")
    article, comments = split_comments(body)
    assert article == "正文。" and comments.startswith("\n\n## 评论里的回复\n\n") and "作者答：答一" in comments
    assert split_comments("没有评论的正文。") == ("没有评论的正文。", "")
    assert split_comments("") == ("", "")


def test_zsxq_import_appends_author_replies(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "777777.md").write_text("拼多多的二季度。\n", encoding="utf-8")
    (tmp_path / "raw" / "777777.json").write_text(json.dumps(_topic(), ensure_ascii=False), encoding="utf-8")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    body = docs[0].body_md
    assert body.startswith("拼多多的二季度。") and "## 评论里的回复" in body
    assert "读者问：Temu 的关税影响有多大？" in body and "作者答：半托管的毛利会被吃掉一半。" in body
    assert "某读者" not in body and "谢谢" not in body
    assert docs[0].raw_meta["comment_replies"] == 2

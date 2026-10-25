"""知识星球评论。

作者本人的回复接在正文后面。导出的「评论（N）」是另一节：阅读页要排成作者、时间、正文，
阅读页和入库都只留方伟自己的评论；空白名字不是方伟。标题前不要堆空换行。
"""

import json
from pathlib import Path

import markdown
import pytest

from researchlib.boilerplate import clean_body
from researchlib.ingest.parse import author_replies, comments_section, parse_archive, zsxq_text
from researchlib.render import inline_image_ocr, render_body
from researchlib.revisions import split_comments
from researchlib.translate.pair import stamp_paragraph_ids
from researchlib.zsxq_comments import display_author

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


# 14879 页底那两则。第二则作者在导出里是一串全角空格，Markdown 留着字面量 **。
_BLANK_NAME = "\u3000" * 7
ARTICLE_14879_COMMENTS = f"""大模型生意的护城河在哪里，明确还不是一个能够回答的话题。

<br>
<br>
<br>

## 评论（3）

- **陈小东**（2026-10-21 11:42）
  个人觉得大模型很难形成护城河，因为只要开放出来用，哪怕是再贵的商用，都会被其他公司用来蒸馏，从而逼近这个模型。

- **方伟Rey**（2026-10-21 11:50）
  护城河还是要看切换成本和应用层。

- **{_BLANK_NAME}**（2026-10-21 11:43）
  claude的fable不包含在20的订阅中，必须按量付费，10美元/M，opus价格的2.5倍
"""


def _section(html: str) -> str:
    marker = 'class="zsxq-comments"'
    start = html.index(marker)
    open_at = html.rindex("<section", 0, start)
    return html[open_at : html.index("</section>", start)]


def test_plain_markdown_leaves_the_14879_comment_bugs():
    """同一套扩展下，导出原文会变成带圆点的列表、字面量星号，标题前还有一串 <br>。"""
    raw = markdown.markdown(ARTICLE_14879_COMMENTS, extensions=["extra", "nl2br", "sane_lists"])
    head, _rest = raw.split("<h2>评论（3）</h2>", 1)
    assert "<ul>" in raw
    assert f"**{_BLANK_NAME}**" in raw
    assert "<br" in head
    assert head.count("<br") >= 3


def test_14879_comments_become_author_time_and_body():
    html = str(render_body(ARTICLE_14879_COMMENTS, {}))
    head = html.split("<section", 1)[0]
    section = _section(html)
    assert "<br" not in head
    assert "<ul" not in html
    assert "**" not in html
    assert "\u3000" not in html
    assert html.count('class="zsxq-comment"') == 1
    assert 'class="zsxq-comment-author">方伟Rey<' in section
    assert "陈小东" not in section
    assert "匿名" not in section
    assert "claude的fable" not in section
    assert "哪怕是再贵的商用" not in section
    assert 'datetime="2026-10-21T11:50"' in section
    assert "护城河还是要看切换成本和应用层" in section
    assert "<h2>评论（1）</h2>" in section
    assert 'class="zsxq-comment-list"' in section
    assert "max-width" not in section


@pytest.mark.parametrize(
    "token",
    [
        "**" + "\u3000" * 7 + "**",
        "**   **",
        "**\u00a0\u00a0**",
        "**\u200b**",
        "\u3000" * 4,
        "****",
        "__" + "\u3000" * 3 + "__",
        "",
        "***",
    ],
)
def test_blank_comment_author_is_hidden(token: str):
    """空白名字不是方伟，整节评论不显示。"""
    line = f"- {token}（2026-10-21 11:43）" if token else "- （2026-10-21 11:43）"
    body = f"正文。\n\n## 评论（1）\n\n{line}\n  一句评论。\n"
    html = str(render_body(body, {}))
    assert "zsxq-comments" not in html
    assert "一句评论。" not in html


def test_real_names_stay_and_author_replies_stay_a_separate_section():
    assert display_author("**陈小东**") == "陈小东"
    assert display_author("**方伟Rey**") == "方伟Rey"
    assert display_author("John Smith") == "John Smith"
    assert display_author("**　　**") == "匿名"
    body = """正文。

## 评论（2）

- **陈小东**（2026-10-21 11:42）
  网友看法不显示。

- **方伟Rey**（2026-10-21 11:50）
  第一段仍然在。

  第二段也在。

## 评论里的回复

读者问：问一

作者答：答一，主站的货币化率还有空间。
"""
    html = str(render_body(body, {}))
    assert html.index("zsxq-comments") < html.index("评论里的回复")
    section = _section(html)
    assert "第一段仍然在。" in section and "第二段也在。" in section
    assert "陈小东" not in section and "网友看法" not in section
    assert "作者答" not in section and "评论里的回复" not in section
    assert "作者答：答一" in html
    assert "zsxq-comments" not in str(render_body("正文。\n\n## 评论里的回复\n\n作者补充：一句。\n", {}))


def test_heading_that_is_not_a_comment_list_is_left_alone():
    body = "正文。\n\n## 评论（2）\n\n这里只是在讨论评论的数量，不是评论列表。\n"
    html = str(render_body(body, {}))
    assert "zsxq-comments" not in html
    assert "这里只是在讨论评论的数量" in html


def test_comment_script_is_stripped():
    body = """正文。

## 评论（1）

- **方伟Rey**（2026-10-21 11:42）
  <script>alert(1)</script>正常文字。
"""
    html = str(render_body(body, {}))
    assert "<script>" not in html
    assert "alert" not in html
    assert "正常文字。" in html


def test_archive_header_is_cleaned_and_comments_still_render():
    raw = """# 帖子

- 作者：王楠
- 时间：2025-11-13 23:07 (UTC+8)
- 类型：talk
- 点赞 2 · 评论 1 · 阅读人数 608
- topic_id：`55188` · 原链接：https://wx.zsxq.com/topic/55188

能不能聊一聊新氧

<br>
<br>

## 评论（1）

- **方伟Rey**（2025-11-13 23:15）
  其实是个风投逻辑
"""
    cleaned = clean_body(raw)
    assert "topic_id" not in cleaned and "## 评论（1）" in cleaned
    html = str(render_body(cleaned, {}))
    head = html.split("<section", 1)[0]
    assert "<br" not in head
    assert "topic_id" not in html and "点赞" not in html
    assert 'class="zsxq-comment-author">方伟Rey<' in html
    assert "其实是个风投逻辑" in html


def _shots_14879() -> list[str]:
    text = (Path(__file__).resolve().parents[1] / "fixtures" / "14879_live_shots.txt").read_text()
    shots: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "====":
            if current:
                shots.append("\n".join(current[2:]).strip())
            current = []
            continue
        current.append(line)
    if current:
        shots.append("\n".join(current[2:]).strip())
    return [shot for shot in shots if shot]


def _page_14879(image_joiner: str) -> str:
    shots = _shots_14879()
    mapping: dict[str, str] = {}
    texts: dict[str, str] = {}
    images: list[str] = []
    for index, shot in enumerate(shots, start=1):
        digest = f"{index:02d}" * 32
        name = f"s{index}.png"
        mapping[name] = f"/media/fangwei-zsxq/ab/{digest}.png"
        texts[digest] = shot
        images.append(f"![]({name})")
    comments = "## 评论（3）" + ARTICLE_14879_COMMENTS.split("## 评论（3）", 1)[1]
    body = "（见图）\n\n" + image_joiner.join(images) + "\n\n<br>\n<br>\n<br>\n\n" + comments
    rendered = stamp_paragraph_ids(str(render_body(body, mapping)))
    return inline_image_ocr(rendered, texts)


def test_14879_shots_keep_dialogue_and_comments_share_the_column():
    """原图链接和说话人不动。评论接在后面只留方伟，标题前没有空 <br>。"""
    page = _page_14879("\n\n")
    assert "国产agents" in page
    assert '<span class="who">方伟：</span>会有一批大模型达到' in page
    assert page.count('class="ocr-reading ocr-dialogue"') == 1
    for number in range(1, 8):
        assert f"原图 {number}" in page
    assert page.index("方伟：") < page.index("原图 1") < page.index("zsxq-comments")
    section = _section(page)
    assert 'class="zsxq-comment-author">方伟Rey<' in section
    assert "陈小东" not in section and "匿名" not in section
    assert "**" not in section and "\u3000" not in section
    gap = page[page.rindex("</figure>") : page.index("zsxq-comments")]
    assert "<br" not in gap
    css = (Path(__file__).resolve().parents[2] / "src/researchlib/web/static/site.css").read_text()
    assert ".reader-grid .prose { max-width: none; }" in css
    rule = css.split(".prose .zsxq-comments {", 1)[1].split("}", 1)[0]
    assert "max-width: none" in rule
    assert "list-style: none" in css.split(".prose .zsxq-comment-list {", 1)[1].split("}", 1)[0]


def test_breaks_between_screenshots_do_not_survive_in_front_of_comments():
    page = _page_14879("<br>\n")
    assert page.count('class="ocr-reading ocr-dialogue"') == 1
    assert "原图 7" in page
    gap = page[page.rindex("</figure>") : page.index("zsxq-comments")]
    assert "<br" not in gap
    assert 'class="zsxq-comment-author">方伟Rey<' in page

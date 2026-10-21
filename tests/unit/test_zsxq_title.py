"""方伟星球：空标题和被省略号截断的标题收成一句完整的话。"""

from pathlib import Path

from markupsafe import Markup

from researchlib.ingest.parse import parse_archive
from researchlib.web.routes import _repair_zsxq_titles
from researchlib.zsxq_title import stored_zsxq_title, title_needs_repair, zsxq_display_title

FULL = "大模型生意的护城河在哪里，明确还不是一个能够回答的话题"
BODY = FULL + "。\n\n作者没有写出为什么现在答不了。\n"
TRUNCATED = "大模型生意的护城河在哪里，明确…"
DIGEST = (
    "方伟认为，大模型生意的护城河在哪里，明确还不是一个能够回答的话题。"
    "正文只给出这一判断，没有说明依据。"
)


def test_complete_sentence_replaces_trailing_ellipsis():
    title = zsxq_display_title(TRUNCATED, body=BODY, when="2024-10-19")
    assert title == FULL
    assert not title.endswith(("…", "..."))
    assert len(title) <= 40
    assert "无标题" not in title


def test_ascii_ellipsis_and_blank_title_use_the_first_sentence():
    assert zsxq_display_title("护城河暂时说不清...", body=BODY) == FULL
    assert zsxq_display_title("   ", body=BODY) == FULL
    assert zsxq_display_title("无标题", body=BODY) == FULL
    assert zsxq_display_title("未命名", body=BODY) == FULL


def test_fragmented_body_uses_digest_sentence_without_author_lead():
    title = zsxq_display_title(TRUNCATED, body=TRUNCATED, summary=DIGEST, when="2024-10-19")
    assert title == FULL
    assert not title.startswith("方伟")
    assert not title.endswith(("…", "...", "。"))


def test_image_post_uses_ocr_before_a_date_fallback():
    ocr = "茅台批价这一周没动，渠道仍在观望，短期不宜提价。"
    title = zsxq_display_title("（见图）", body="（见图）\n", ocr=ocr, when="2024-10-19")
    assert title == "茅台批价这一周没动，渠道仍在观望，短期不宜提价"
    assert "读者帖" not in title


def test_thin_post_falls_back_to_date_and_snippet():
    title = zsxq_display_title("", body="正文。\n", when="2024-11-28")
    assert title == "2024-11-28 正文"
    assert not title.endswith(("…", "..."))
    assert zsxq_display_title("", body="", when="2024-11-28") == "2024-11-28 短帖"
    assert zsxq_display_title("", body="", when="") == "短帖"


def test_long_sentence_stops_on_a_clause_without_ellipsis():
    sentence = FULL + "，因为账上还没有拆开的利润表可以拿来核对收入质量"
    title = zsxq_display_title("…", body=sentence + "。\n")
    assert len(title) <= 40
    assert not title.endswith(("…", "...", "，"))
    assert title.startswith("大模型生意的护城河在哪里")


def test_usable_title_is_left_alone():
    assert title_needs_repair("茅台短帖") is False
    assert zsxq_display_title("茅台短帖", body=BODY) == "茅台短帖"
    assert stored_zsxq_title("茅台短帖", BODY) is None
    assert stored_zsxq_title(TRUNCATED, BODY) == FULL
    assert stored_zsxq_title("（见图）", "（见图）\n") is None


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql, _params):
        return self

    def fetchall(self):
        return self.rows


def test_cards_repair_fangwei_titles_and_leave_other_sources():
    fangwei = {
        "id": 7,
        "source_id": "fangwei-zsxq",
        "plain_title": TRUNCATED,
        "title": Markup(TRUNCATED),
        "summary": "",
        "date": "2024-10-19",
    }
    other = {
        "id": 8,
        "source_id": "fangwei-mp",
        "plain_title": TRUNCATED,
        "title": TRUNCATED,
        "summary": "",
        "body_text": BODY,
        "date": "2024-10-19",
    }
    _repair_zsxq_titles(_Conn([{"id": 7, "body_text": BODY, "ocr_text": ""}]), [fangwei, other])
    assert fangwei["title"] == FULL
    assert fangwei["plain_title"] == FULL
    assert not isinstance(fangwei["title"], Markup)
    assert other["title"] == TRUNCATED


def test_card_without_body_uses_digest_sentence():
    card = {
        "id": 9,
        "source_id": "fangwei-zsxq",
        "plain_title": "",
        "title": "",
        "summary": DIGEST,
        "date": "2024-10-19",
    }
    _repair_zsxq_titles(_Conn([{"id": 9, "body_text": TRUNCATED, "ocr_text": ""}]), [card])
    assert card["plain_title"] == FULL
    assert not card["title"].endswith("…")


# 14885：库内标题是带中文引号的「图片」，正文空，摘要只说明没正文，料在 OCR。
PICTURE = "「图片」"
PICTURE_NOTE = "正文为空，标题仅为「图片」。"
PICTURE_OCR = "烟蒂股这轮又被翻出来了，中概互联网的现金流比故事好看。\n后面接着聊自由现金流。\n"
PICTURE_TITLE = "烟蒂股这轮又被翻出来了，中概互联网的现金流比故事好看"

# 14865：标题以省略号结尾；正文是归档头加半截标题；摘要是完整句；OCR 是另一则配售新闻。
XIAOMI = "跟去年小米的剧本总是似曾相识+\n..."
XIAOMI_BODY = """# 帖子

- 作者：方伟
- 时间：2025-11-13 23:07 (UTC+8)
- 类型：talk
- 点赞 2 · 评论 1 · 阅读人数 608

跟去年小米的剧本总是似曾相识+
...
似曾相识 巴菲特
"""
XIAOMI_DIGEST = "方伟认为，今年小米的走势和去年的剧本很像。"
XIAOMI_TITLE = "今年小米的走势和去年的剧本很像"
XIAOMI_OCR = "智谱宣布完成配售，募资用于大模型训练和算力采购。"


def _no_ellipsis(title: str) -> None:
    assert title
    assert not title.startswith(("…", "...", "⋯", "．．．"))
    assert not title.endswith(("…", "...", "⋯", "．．．"))
    assert "…" not in title and "..." not in title


def test_quoted_placeholders_need_repair_and_use_ocr():
    assert title_needs_repair("「图片」")
    assert title_needs_repair("「见图」")
    assert title_needs_repair("「 图片 」")
    assert title_needs_repair("“无标题”")
    assert title_needs_repair("『未命名』")
    assert title_needs_repair("《图片》")
    assert title_needs_repair("「茅台批价」") is False
    # 原标题中间的停顿不是截断，不用改写。
    assert title_needs_repair("他说……然后停了") is False
    assert zsxq_display_title("他说……然后停了", body="另一句完整的话在正文里。") == "他说……然后停了"
    title = zsxq_display_title(PICTURE, body="", summary=PICTURE_NOTE, ocr=PICTURE_OCR, when="2024-10-19")
    assert title == PICTURE_TITLE
    _no_ellipsis(title)
    assert "正文为空" not in title
    assert zsxq_display_title("「见图」", body="（见图）\n", ocr=PICTURE_OCR) == PICTURE_TITLE
    card = {
        "id": 14885,
        "source_id": "fangwei-zsxq",
        "plain_title": PICTURE,
        "title": PICTURE,
        "summary": PICTURE_NOTE,
        "body_text": "",
        "date": "2024-10-19",
    }
    _repair_zsxq_titles(_Conn([{"id": 14885, "body_text": "", "ocr_text": PICTURE_OCR}]), [card])
    assert card["title"] == PICTURE_TITLE
    assert card["plain_title"] == PICTURE_TITLE


# 线上脏摘要：空语音套话，或在复述「这篇没写什么」。不能压过图片里的判断，也不能压过原标题主干。
DIRTY_DUAN = "（没有段永平本人的话）"
DIRTY_XIAOMI = "作者开篇判断与去年小米剧本似曾相识，文中未给对照机制。"
XIAOMI_STEM = "跟去年小米的剧本总是似曾相识"


def test_narration_summary_is_not_a_title():
    assert title_needs_repair(DIRTY_DUAN)
    assert title_needs_repair("没有段永平本人的话")
    assert title_needs_repair("没有他本人的话。")
    picture = zsxq_display_title(PICTURE, body="", summary=DIRTY_DUAN, ocr=PICTURE_OCR, when="2024-10-19")
    assert picture == PICTURE_TITLE
    assert "段永平" not in picture
    assert picture != "图片"
    # 套话写进标题栏时也要让开，用图片里的第一句。
    assert zsxq_display_title(DIRTY_DUAN, body="", summary=DIRTY_DUAN, ocr=PICTURE_OCR) == PICTURE_TITLE
    for summary in (
        "正文几乎只有半截标题，看不出判断。",
        "文中未给对照机制。",
        "作者开篇判断与去年的剧本相似。",
        "没有作者本人的话",
        "（没有他本人的话）",
    ):
        got = zsxq_display_title("「图片」", body="（见图）\n", summary=summary, ocr=PICTURE_OCR)
        assert got == PICTURE_TITLE, summary
    card = {
        "id": 14885,
        "source_id": "fangwei-zsxq",
        "plain_title": PICTURE,
        "title": PICTURE,
        "summary": DIRTY_DUAN,
        "date": "2024-10-19",
    }
    _repair_zsxq_titles(_Conn([{"id": 14885, "body_text": "", "ocr_text": PICTURE_OCR}]), [card])
    assert card["title"] == PICTURE_TITLE
    assert "段永平" not in card["plain_title"]


def test_ellipsis_stem_beats_a_narration_summary():
    """14865：去掉省略号后用原标题主干，不用复述摘要，也不用另一张配售截图。"""
    title = zsxq_display_title(
        XIAOMI,
        body=XIAOMI_BODY,
        summary=DIRTY_XIAOMI,
        ocr=XIAOMI_OCR,
        when="2025-11-13",
    )
    assert title == XIAOMI_STEM
    _no_ellipsis(title)
    assert "作者开篇" not in title
    assert "文中未给" not in title
    assert "正文几乎" not in title
    assert "智谱" not in title
    assert "巴菲特" not in title
    card = {
        "id": 14865,
        "source_id": "fangwei-zsxq",
        "plain_title": XIAOMI,
        "title": XIAOMI,
        "summary": DIRTY_XIAOMI,
        "date": "2025-11-13",
    }
    _repair_zsxq_titles(
        _Conn([{"id": 14865, "body_text": XIAOMI_BODY, "ocr_text": XIAOMI_OCR}]),
        [card],
    )
    assert card["title"] == XIAOMI_STEM
    assert card["plain_title"] == XIAOMI_STEM


def test_truncated_title_prefers_digest_over_unrelated_ocr():
    assert title_needs_repair(XIAOMI)
    title = zsxq_display_title(
        XIAOMI,
        body=XIAOMI_BODY,
        summary=XIAOMI_DIGEST,
        ocr=XIAOMI_OCR,
        when="2025-11-13",
    )
    assert title == XIAOMI_TITLE
    _no_ellipsis(title)
    assert "智谱" not in title
    assert "巴菲特" not in title
    assert not title.startswith("…")
    # 正文几乎只有半截标题时，不要拼成「... 似曾相识 巴菲特」。
    glued = zsxq_display_title(XIAOMI, body="...\n似曾相识 巴菲特\n", summary=XIAOMI_DIGEST, ocr=XIAOMI_OCR)
    assert glued == XIAOMI_TITLE
    # 没有摘要时，也不要用无关配售新闻；去掉省略号后的半截标题可以兜底。
    stem = zsxq_display_title(XIAOMI, body=XIAOMI_BODY, ocr=XIAOMI_OCR)
    assert stem == "跟去年小米的剧本总是似曾相识"
    _no_ellipsis(stem)


def test_zsxq_import_stores_a_complete_sentence(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "888888.md").write_text(BODY, encoding="utf-8")
    (tmp_path / "raw" / "888888.json").write_text(
        '{"topic_id": 888888, "create_time": "2024-10-19T05:29:00+00:00", "title": "大模型生意的护城河在哪里，明确…"}',
        encoding="utf-8",
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    assert docs[0].title == FULL
    assert not docs[0].title.endswith("…")

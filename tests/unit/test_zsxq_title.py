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

"""方伟星球：空标题和被省略号截断的标题收成一句完整的话。"""

from pathlib import Path

from markupsafe import Markup

from researchlib.ingest.parse import header_topic_id, parse_archive
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


def test_fragmented_body_keeps_the_title_stem_over_a_digest():
    """去掉省略号后还是一句可用的原标题，不用摘要把后半句补回来。"""
    title = zsxq_display_title(TRUNCATED, body=TRUNCATED, summary=DIGEST, when="2024-10-19")
    assert title == "大模型生意的护城河在哪里"
    assert not title.startswith("方伟")
    assert not title.endswith(("…", "...", "。"))
    # 原标题本身没有可用句时，摘要的判断句仍然可以当标题。
    assert zsxq_display_title("…", body="…", summary=DIGEST) == FULL


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
    # 正文也是半截时，入库写成去掉省略号的主干，不再把「…」写进库。
    assert stored_zsxq_title(TRUNCATED, TRUNCATED) == "大模型生意的护城河在哪里"


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


# 14865 线上 h1 曾被这句无关配售截图盖住。
LIVE_OCR = "智谱完成约314.1亿港元配售，资金主要用于基座模型研发、算力基建"


def test_14865_stem_beats_dirty_summary_and_unrelated_ocr():
    """原标题有省略号，摘要是复述或配售截图那句，OCR 也是配售：仍用原标题主干。"""
    assert title_needs_repair(XIAOMI)
    title = zsxq_display_title(
        XIAOMI,
        body=XIAOMI_BODY,
        summary=DIRTY_XIAOMI,
        ocr=LIVE_OCR,
        when="2025-11-13",
    )
    assert title == XIAOMI_STEM
    _no_ellipsis(title)
    assert "智谱" not in title
    assert "配售" not in title
    assert "作者开篇" not in title
    assert "巴菲特" not in title
    # 摘要被重写成截图那句时，也不能盖过原标题。
    assert zsxq_display_title(XIAOMI, body=XIAOMI_BODY, summary=LIVE_OCR, ocr=LIVE_OCR) == XIAOMI_STEM
    assert zsxq_display_title(XIAOMI, body=XIAOMI_BODY, summary=XIAOMI_DIGEST, ocr=LIVE_OCR) == XIAOMI_STEM
    glued = zsxq_display_title(XIAOMI, body="...\n似曾相识 巴菲特\n", summary=LIVE_OCR, ocr=LIVE_OCR)
    assert glued == XIAOMI_STEM
    card = {
        "id": 14865,
        "source_id": "fangwei-zsxq",
        "plain_title": XIAOMI,
        "title": XIAOMI,
        "summary": DIRTY_XIAOMI,
        "date": "2025-11-13",
    }
    _repair_zsxq_titles(
        _Conn([{"id": 14865, "body_text": XIAOMI_BODY, "ocr_text": LIVE_OCR}]),
        [card],
    )
    assert card["title"] == XIAOMI_STEM
    assert card["plain_title"] == XIAOMI_STEM
    # 「图片」这种占位仍然读图片里的字，不退回配售以外的空标题。
    picture = zsxq_display_title("「图片」", body="", summary=DIRTY_XIAOMI, ocr=LIVE_OCR)
    assert picture == LIVE_OCR
    assert picture != XIAOMI_STEM


def test_ellipsis_tucked_before_a_closing_quote_still_needs_repair():
    assert title_needs_repair("护城河在哪里…」")
    assert title_needs_repair("护城河在哪里...")
    title = zsxq_display_title("护城河在哪里…」", body="护城河在哪里…」")
    assert title == "护城河在哪里"
    assert not title.endswith(("…", "」", "..."))
    assert stored_zsxq_title("茅…", "茅…", summary="正文几乎只有半截。", ocr="茅台批价这一周没动，渠道仍在观望。") == (
        "茅台批价这一周没动，渠道仍在观望"
    )


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


def test_zsxq_import_stores_a_stem_when_the_body_is_also_cut(tmp_path: Path):
    """新抓的帖若正文也是半截，标题不再以省略号入库。"""
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "888889.md").write_text(TRUNCATED + "\n", encoding="utf-8")
    (tmp_path / "raw" / "888889.json").write_text(
        '{"topic_id": 888889, "create_time": "2024-10-19T05:29:00+00:00", "title": "大模型生意的护城河在哪里，明确…"}',
        encoding="utf-8",
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    assert docs[0].title == "大模型生意的护城河在哪里"
    assert not docs[0].title.endswith("…")
    assert not title_needs_repair(docs[0].title)


# 星球接口给的标题只截正文开头十几个字，不带省略号（线上「……持有60.6」「……把钱交给」这类）。
CUT_TITLE = "公司通过 B 类股持有60.6"
CUT_SENTENCE = "公司通过 B 类股持有60.6% 投票权，还要再发一类没有投票权的股票"
CUT_BODY = CUT_SENTENCE + "。\n\n后面一段讲资本开支。\n"
ARCHIVE_HEADER = """- 作者：方伟Rey
- 时间：2026-10-19 18:28 (UTC+8)
- 类型：talk
- 点赞 5 · 评论 0 · 阅读人数 616
- topic_id：`12345678901234567` · 原链接：https://wx.zsxq.com/topic/12345678901234567
"""


def test_api_title_cut_mid_sentence_is_completed_from_the_body():
    # 只看标题看不出截断，要对着正文。
    assert title_needs_repair(CUT_TITLE) is False
    assert title_needs_repair(CUT_TITLE, CUT_BODY)
    assert zsxq_display_title(CUT_TITLE, body=CUT_BODY) == CUT_SENTENCE
    assert stored_zsxq_title(CUT_TITLE, CUT_BODY) == CUT_SENTENCE
    # 正文开头先重复一遍标题（导出的标题行），或者标题行带着「...」，都看下一行。
    assert zsxq_display_title(CUT_TITLE, body=f"# {CUT_TITLE}\n\n{CUT_BODY}") == CUT_SENTENCE
    assert zsxq_display_title(CUT_TITLE, body=f"{CUT_TITLE}...\n\n{CUT_BODY}") == CUT_SENTENCE
    # 中间隔着归档头也认得出。
    assert stored_zsxq_title(CUT_TITLE, f"# {CUT_TITLE}\n\n{ARCHIVE_HEADER}\n{CUT_BODY}") == CUT_SENTENCE
    # 问句补全后留着问号；太长的句子和别处一样收在逗号处。
    question = "请教一下，跳出行业来看，什么是好的商业模式？\n"
    assert zsxq_display_title("请教一下，跳出行业来看", body=question) == "请教一下，跳出行业来看，什么是好的商业模式？"
    long = "看估值时一定要把回购算进去，" + "因为同样的钱在低估值时能买回更多股份，留下来的股东更受益。"
    title = zsxq_display_title("看估值时一定要把回购算", body=long)
    assert title == "看估值时一定要把回购算进去，因为同样的钱在低估值时能买回更多股份"
    assert len(title) <= 40


def test_title_that_ends_where_the_body_breaks_is_left_alone():
    # 正文在标题这里就断句了，或者标题自己占一行：标题是完整的。
    assert title_needs_repair("瑞幸的门店是比较多的", "瑞幸的门店是比较多的。\n\n![](chat.png)\n") is False
    assert title_needs_repair("企业文化", "# 企业文化\n\n很多人问企业文化怎么看。\n") is False
    assert title_needs_repair("问：怎么看短剧侵占游戏时长？", "问：怎么看短剧侵占游戏时长？\n分享个数据\n") is False
    # 正文不从标题开始，或者标题太短，都不当截断。
    assert title_needs_repair("门店扩张", "今天说说门店扩张，还在继续。\n") is False
    assert title_needs_repair("茅台", "茅台批价这一周没动。\n") is False
    assert zsxq_display_title("瑞幸的门店是比较多的", body="瑞幸的门店是比较多的。\n") == "瑞幸的门店是比较多的"
    assert stored_zsxq_title("企业文化", "# 企业文化\n\n很多人问企业文化怎么看。\n") is None


def test_archive_header_never_becomes_a_title():
    """线上 14885：图片帖入库时把归档头读成了标题「「图片」 - 作者：方伟Rey - 时间：2026-10-19 18」。"""
    body = f"# 「图片」\n\n{ARCHIVE_HEADER}\n![](../../images/a.jpg)\n"
    assert stored_zsxq_title("「图片」", body) is None
    assert zsxq_display_title("「图片」", body=body, ocr=PICTURE_OCR) == PICTURE_TITLE
    # 库里已经是这种标题的：切掉混进来的归档头，按前面的原标题重新取。
    junk = "「图片」 - 作者：方伟Rey - 时间：2026-10-19 18"
    assert title_needs_repair(junk)
    assert zsxq_display_title(junk, body=body, ocr=PICTURE_OCR) == PICTURE_TITLE
    asked = "问：10PE的蜜雪，是否足够有吸引力了 - 作者：王小明 - 时间"
    assert title_needs_repair(asked)
    assert stored_zsxq_title(asked, "问：10PE的蜜雪，是否足够有吸引力了？\n") == "问：10PE的蜜雪，是否足够有吸引力了"
    # 标题里正常的「 - 」不算。
    assert title_needs_repair("估值 - 时间是朋友") is False


def test_zsxq_import_completes_a_cut_api_title(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "12345678901234567.md").write_text(
        f"# {CUT_TITLE}\n\n{ARCHIVE_HEADER}\n{CUT_BODY}", encoding="utf-8"
    )
    (tmp_path / "raw" / "12345678901234567.json").write_text(
        '{"topic_id": 12345678901234567, "create_time": "2026-10-19T10:28:00+00:00", "title": "' + CUT_TITLE + '"}',
        encoding="utf-8",
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    assert docs[0].title == CUT_SENTENCE


def test_zsxq_import_keeps_the_archive_header_out_of_the_title(tmp_path: Path):
    """10 月 20 日以前的导入没读到 raw/：标题只能从 md 取，话题号从归档头取。"""
    (tmp_path / "md").mkdir()
    (tmp_path / "md" / "2026-10-19_图片.md").write_text(
        f"# 「图片」\n\n{ARCHIVE_HEADER}\n![](../../images/a.jpg)\n", encoding="utf-8"
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    assert docs[0].title == "「图片」"
    assert docs[0].external_id == "2026-10-19_图片"
    assert docs[0].raw_meta["topic_id"] == "12345678901234567"


def test_header_topic_id_reads_only_the_archive_header():
    assert header_topic_id(ARCHIVE_HEADER) == "12345678901234567"
    assert header_topic_id("topic_id: 765432198") == "765432198"
    assert header_topic_id("正文里没有话题号。") is None
    # 正文很靠后才提到的话题号不算。
    assert header_topic_id("正文。" * 1000 + "topic_id：`12345678901234567`") is None

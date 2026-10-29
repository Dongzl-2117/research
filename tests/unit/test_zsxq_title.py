"""方伟星球：空标题和被省略号截断的标题收成一句完整的话。"""

from pathlib import Path

from markupsafe import Markup

from researchlib.ingest.parse import header_topic_id, parse_archive
from researchlib.web.routes import _repair_zsxq_titles
from researchlib.zsxq_title import stored_zsxq_title, title_needs_ai, title_needs_repair, zsxq_display_title

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


def test_ai_only_for_shells_placeholders_not_mid_sentence_cuts():
    assert title_needs_ai("泡泡玛特：")
    assert title_needs_ai("无标题")
    assert title_needs_ai("护城河…")
    assert title_needs_ai("")
    # 识图前写成的空壳导读。旧正则对不上「没有可读文字」，回写会跳过。
    shell = "正文除标题「图片」外没有可读文字，无法提炼作者的核心判断及其依据"
    assert title_needs_ai(shell)
    assert title_needs_repair(shell)
    assert title_needs_ai("本文正文为空")
    assert title_needs_ai("本文正文为空，除标题《关于携程与Booking》外没有任何内容")
    assert title_needs_ai("关于携程与Booking") is False
    assert title_needs_repair("关于携程与Booking") is False
    assert title_needs_ai("正文为空洞的口号") is False
    # 错字只抛光，不起题。
    assert title_needs_ai("AI算力是一个很新都东西") is False
    # 半句截断对着正文补，不打 flash。
    assert title_needs_ai("YouTube的护城河类型是平") is False
    assert title_needs_repair(
        "YouTube的护城河类型是平",
        "YouTube的护城河类型是平台网络效应，Netflix只是规模效应。\n",
    )


def test_company_shell_colon_titles_need_repair():
    assert title_needs_repair("泡泡玛特：")
    assert title_needs_repair("西部矿业：")
    assert title_needs_repair("看数：")
    assert title_needs_repair("YouTube的护城河类型是平", "YouTube的护城河类型是平台网络效应，Netflix只是规模效应。\n")
    # 完整句不以冒号壳论。
    assert title_needs_repair("茅台短帖") is False
    # 有摘要时确定性规则也能收成一句（不靠 flash）。
    digest = "方伟认为泡泡玛特下跌是自身问题，不是名创或行业竞争抢份额。"
    title = zsxq_display_title("泡泡玛特：", body="泡泡玛特：", summary=digest)
    assert "泡泡玛特" in title
    assert not title.endswith(("：", ":"))
    assert not title_needs_repair(title)


def test_obvious_title_typos_are_polished():
    from researchlib.textutil import fix_obvious_typos
    from researchlib.zsxq_title import polish_title_typos

    raw = "AI算力是一个很新都东西"
    assert polish_title_typos(raw) == fix_obvious_typos(raw) == "AI算力是一个很新的东西"
    # 标题模块不另维护错字表，共享表里的另一条也要生效。
    assert polish_title_typos("存在运用杠杆的好处") == "存在运营杠杆的好处"
    assert title_needs_repair(raw)
    assert title_needs_ai(raw) is False
    assert zsxq_display_title(raw, body="问：星主好\n") == "AI算力是一个很新的东西"
    assert stored_zsxq_title(raw, "问：星主好\n") == "AI算力是一个很新的东西"
    # 错字若同时是正文半句，仍补全那一句，再走同一张表。
    cut = "存在运用杠杆的好"
    body = "存在运用杠杆的好处很多，毛利率会上去。\n"
    done = "存在运营杠杆的好处很多，毛利率会上去"
    assert zsxq_display_title(cut, body=body) == done
    assert stored_zsxq_title(cut, body) == done


# 23984：库内标题停在「有两...」，正文第一句已经写完，而且不到 40 字。
# 正文那句开头的前引号要到后面几段才收，标题里只剩前一半，去掉。
STORY_STORED = "“这个故事我琢磨了很多年，有两..."
STORY_SENTENCE = "这个故事我琢磨了很多年，有两点感触很深"
STORY_BODY = (
    "# “这个故事我琢磨了很多年，有两...\n\n"
    "“这个故事我琢磨了很多年，有两点感触很深：\n"
    "第一，巴菲特当时是认真思考过的，无疑在心里权衡赔率与得失。\n"
)


def test_ellipsis_mid_phrase_completes_from_the_body_sentence():
    """省略号停在「有两」时，用正文里那句完整的话，不收成「有两」或逗号前的半句。"""
    assert title_needs_repair(STORY_STORED)
    # 分句以冒号收尾不是「泡泡玛特：」那种壳。
    assert title_needs_repair(STORY_SENTENCE + "：") is False
    title = zsxq_display_title(STORY_STORED, body=STORY_BODY)
    assert title == STORY_SENTENCE
    assert len(title) <= 40
    assert not title.endswith(("…", "...", "有两", "："))
    assert "第一" not in title
    assert stored_zsxq_title(STORY_STORED, STORY_BODY) == STORY_SENTENCE
    # 正文也只有半截时，收到上一个逗号，不把没写完的「有两点感触很深但是…」留着。
    cut = "这个故事我琢磨了很多年，有两点感触很深但是后面还没写完..."
    assert zsxq_display_title(cut, body=cut) == "这个故事我琢磨了很多年"
    assert "有两点感" not in zsxq_display_title(cut, body=cut)


# 24002：库内标题被截断，话题壳重复，后面粘着评论列表的界面。
LIVE_24002 = "泡泡玛特： 泡泡玛特： 评论（1） - 流年（2026-10-28 09"


def test_comment_list_ui_crumbs_need_repair_and_ai():
    """评论（N）、半角括号、名字加被截断的钟点，都要修。只剩话题壳时才打 flash。"""
    assert title_needs_repair(LIVE_24002)
    assert title_needs_ai(LIVE_24002)
    ascii_variant = "泡泡玛特： 泡泡玛特： 评论(1) - 流年(2026-10-28 09:12)"
    assert title_needs_repair(ascii_variant)
    assert title_needs_ai(ascii_variant)
    glued = "西部矿业：西部矿业：评论（3）-李四（2026-10-27 19"
    assert title_needs_repair(glued)
    assert title_needs_ai(glued)
    # 评论条数被截掉了，重复的话题壳仍贴着评论者。
    author_only = "泡泡玛特： 泡泡玛特： - 流年（2026-10-28 09"
    assert title_needs_repair(author_only)
    assert title_needs_ai(author_only)
    shown = zsxq_display_title(LIVE_24002, body="泡泡玛特：\n", when="2026-10-28")
    assert "流年" not in shown
    assert "评论（" not in shown and "评论(" not in shown
    assert stored_zsxq_title(LIVE_24002, "泡泡玛特：\n") is None


def test_comment_ui_tail_strips_when_a_clean_stem_remains():
    """残屑前面还有一句，就切掉，不改用正文里的另一句，也不打 flash。"""
    raw = "茅台批价这一周没动 评论（2） - 王小明（2026-10-28 10:01）"
    body = "另一句完全不同的判断写在正文里，渠道仍在观望。\n"
    assert title_needs_repair(raw)
    assert title_needs_ai(raw) is False
    assert zsxq_display_title(raw, body=body) == "茅台批价这一周没动"
    assert stored_zsxq_title(raw, body) == "茅台批价这一周没动"
    ascii_stem = "渠道仍在观望 评论(1) - 李四(2026-10-28 10:01)"
    assert title_needs_repair(ascii_stem)
    assert title_needs_ai(ascii_stem) is False
    assert zsxq_display_title(ascii_stem, body=body) == "渠道仍在观望"


def test_comment_word_in_a_real_title_is_left_alone():
    """只是提到评论，或括号里不是评论列表的钟点，不动。"""
    kept = (
        "怎么看评论区里的分歧",
        "评论不等于观点",
        "他在评论里补充了茅台批价",
        "他在评论（1）里补充了看法",
        "点赞 2 · 评论 1 · 阅读人数 608",
        "复盘 - 王小明（2024年报要点）",
        "估值 - 时间是朋友",
        "估值 - 时间是朋友（2026-10-28）",
        "巴菲特致股东信（2026-10-28）",
        "这篇评论（精选）不算界面",
    )
    for title in kept:
        assert title_needs_repair(title) is False, title
        assert title_needs_ai(title) is False, title
        assert zsxq_display_title(title, body="另一句完全不同的正文在这里。") == title
    assert zsxq_display_title("", body="怎么看评论区里的分歧，渠道还在观望。\n") == "怎么看评论区里的分歧，渠道还在观望"


def test_comment_ui_title_can_still_use_a_real_sentence():
    """壳上粘着残屑时，摘要或图片里的完整句仍能当标题，残屑本身不能。"""
    digest = "方伟认为泡泡玛特下跌是自身问题，不是名创抢份额。"
    from_digest = zsxq_display_title(LIVE_24002, body="泡泡玛特：\n", summary=digest)
    assert from_digest == "泡泡玛特下跌是自身问题，不是名创抢份额"
    assert "流年" not in from_digest
    ocr = (
        "泡泡玛特： 泡泡玛特： 评论（1） - 流年（2026-10-28 09:00）\n"
        "泡泡玛特下跌是自身问题，不是名创抢份额。\n"
    )
    from_ocr = zsxq_display_title(LIVE_24002, body="泡泡玛特：\n", ocr=ocr)
    assert from_ocr == "泡泡玛特下跌是自身问题，不是名创抢份额"
    assert stored_zsxq_title(LIVE_24002, "泡泡玛特：\n", ocr=ocr) == from_ocr


def test_truncated_heading_is_not_repeated_under_the_repaired_title():
    from researchlib.textutil import strip_duplicate_title

    shown = strip_duplicate_title(STORY_BODY, STORY_SENTENCE)
    assert "有两..." not in shown
    assert shown.lstrip().startswith("“" + STORY_SENTENCE)
    other = "# 茅台批价这一周没动\n\n正文。\n"
    assert strip_duplicate_title(other, STORY_SENTENCE) == other
    # 23989：页面标题去掉了落单的前引号，标题行还带着，也算同一行。
    lonely = "# “估值越低，同样的钱能买回的股份越多，对留下来的股东越有利\n\n正文。\n"
    assert strip_duplicate_title(lonely, "估值越低，同样的钱能买回的股份越多，对留下来的股东越有利").strip() == "正文。"
    # 配得上对的符号不算落单，去掉以后不一样就是两回事。
    paired = "# 《穷查理宝典》读后感\n\n正文。\n"
    assert strip_duplicate_title(paired, "穷查理宝典读后感") == paired


def test_lonely_quote_marks_are_dropped():
    """截出来的句子只剩前引号（23989）：去掉落单的那个，原标题照用，不打 flash。"""
    title = "“估值越低，同样的钱能买回的股份越多，对留下来的股东越有利"
    body = title + "。巴菲特一直说，作为长期股东，你应该希望自己持有的公司股价长期低迷。\n"
    assert title_needs_repair(title)
    assert not title_needs_ai(title)
    fixed = zsxq_display_title(title, body=body)
    assert fixed == "估值越低，同样的钱能买回的股份越多，对留下来的股东越有利"
    assert stored_zsxq_title(title, body) == fixed
    # 配得上对的不动，开头的编号也不算落单。
    for kept in ("《金融时报》说的「护城河」还在", "1）估值的三个误区", "一）先看现金流"):
        assert not title_needs_repair(kept), kept
        assert zsxq_display_title(kept) == kept
    # 夹在一对引号中间落单的，只去掉那一个。
    assert zsxq_display_title("「腾讯“说的护城河」还在") == "「腾讯说的护城河」还在"
    # 长句收在逗号处，后引号落在后半句里：前引号也去掉。
    quoted = "“估值越低，同样的钱能买回的股份越多，对留下来的股东越有利，管理层应该在低位多回购”，这是今天的结论。\n"
    assert "“" not in zsxq_display_title("", body=quoted)


def test_other_zsxq_sources_repair_titles_on_cards():
    """乘风破浪也是星球：卡片上带省略号的标题照样补。按来源的 kind 认，不写死来源 id。"""
    from researchlib.zsxq_title import repairs_source

    assert repairs_source("fangwei-zsxq")
    assert repairs_source("chengfengpolang", "zsxq")
    assert not repairs_source("chengfengpolang")
    assert not repairs_source("fangwei-mp", "wechat_mp")
    # 1250：正文第一行就是去掉省略号的标题，用这一行，不换成正文第一句。
    body = (
        "Temu：值得重视的关税利好\n"
        "中美这次国事访问后达成的对等降税安排，主要是针对几类商品：小家电、玩具、节庆装饰品等。\n"
    )
    card = {
        "id": 1250,
        "source_id": "chengfengpolang",
        "source_kind": "zsxq",
        "plain_title": "Temu：值得重视的关税利好 ...",
        "title": "Temu：值得重视的关税利好 ...",
        "summary": "",
        "date": "2026-09-27",
    }
    _repair_zsxq_titles(_Conn([{"id": 1250, "body_text": body, "ocr_text": ""}]), [card])
    assert card["plain_title"] == "Temu：值得重视的关税利好"
    assert stored_zsxq_title("Temu：值得重视的关税利好 ...", body) == "Temu：值得重视的关税利好"
    # 正文里没有这一行时照旧：截断的半句不当标题。
    assert zsxq_display_title("Temu：值得重视的关税利好 ...", body=body.split("\n", 1)[1]) != (
        "Temu：值得重视的关税利好"
    )

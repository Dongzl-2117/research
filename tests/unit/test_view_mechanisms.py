"""OCR 旁路、引用规范化、补抽缺口的纯逻辑、星球发帖人、论点的边和重叠。"""

from researchlib.author_voice import assign_poster_role, is_guest
from researchlib.claims import claim_side, looks_like_list, plain_cite, quote_ok, same_repost
from researchlib.digest import PROMPT_VERSION, digest_due
from researchlib.excerpt import view_windows
from researchlib.ocr import append_ocr, compose_ocr_text
from researchlib.theses import parse_theses
from researchlib.views import locate_quote, parse_views


class _Conn:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return [{"ord": 3, "body": "现阶段不宜提价，批价已经很高。"}]


def test_short_article_digest_is_due_when_ocr_arrives():
    current = {"prompt_version": PROMPT_VERSION, "head_only": False, "ocr_hash": ""}
    short = {"ocr_hash": "abc", "body_text": "瑞幸古茗是比较头部的。"}
    assert digest_due(short, current)
    assert digest_due(short, None)
    assert not digest_due(short, {**current, "ocr_hash": "abc"})
    assert not digest_due({"ocr_hash": "abc", "body_text": "字" * 400}, current)
    assert not digest_due({"ocr_hash": "", "body_text": "只有一句。"}, current)


def test_ocr_text_is_appended_for_views_and_digest():
    pieces = [" logo ", "茅台批价这一周没动，渠道仍在观望提价。", "x"]
    text = compose_ocr_text(pieces)
    assert "茅台批价" in text and "logo" not in text
    reading = append_ocr("（这是一张图片）", text)
    assert reading.startswith("（这是一张图片）") and "【图片里的字】" in reading and "茅台批价" in reading
    assert append_ocr("只有正文", "") == "只有正文"


def test_quote_ignores_newlines_and_rejects_empty():
    body = "作者写到：现阶段不宜提价，批价已经很高。"
    quote, chunk = locate_quote(_Conn(), 1, body, "现阶段\n不宜提价，批价已经很高。")
    assert chunk == 3 and plain_cite(quote) == plain_cite("现阶段不宜提价，批价已经很高。")
    assert locate_quote(_Conn(), 1, body, "\n\n") == ("", None)
    assert locate_quote(_Conn(), 1, body, "") == ("", None)
    assert not quote_ok("  \n") and quote_ok("现阶段不宜提价")


def test_long_text_is_split_before_extraction():
    chunks = [("", "腾讯" + "甲" * 8000), ("", "乙" * 8000), ("", "腾讯收在结尾，广告还有空间。")]
    body = "\n".join(text for _heading, text in chunks)
    windows = view_windows(body, chunks, ["腾讯"], window=9000)
    assert len(windows) >= 2
    assert any("广告还有空间" in window for window in windows)
    assert all(len(window) <= 12000 for window in windows)


def test_guest_post_is_not_the_star_owner():
    guest = assign_poster_role({"poster_name": "某读者", "poster_id": "9"}, "方伟")
    owner = assign_poster_role({"poster_name": "方伟（星主）"}, "方伟")
    unknown = assign_poster_role({}, "方伟")
    assert is_guest(guest) and guest["poster_role"] == "guest"
    assert owner["poster_role"] == "owner" and not is_guest(owner)
    assert "poster_role" not in unknown


def test_list_mention_is_not_about_the_company():
    text = (
        '{"泡泡玛特": {"about": true, "judgment": "新消费四小龙包括泡泡玛特、名创优品、泡泡等",'
        ' "stance": "正面", "quote": "包括泡泡玛特、名创优品"}}'
    )
    found = parse_views(text, {"泡泡玛特": 1})
    assert found[1]["about"] is False
    assert looks_like_list("例如茅台、五粮液都涨了", "茅台")


def test_price_hike_opposition_is_against_even_if_the_author_is_bullish():
    assert claim_side("茅台仍有提价空间", "现阶段不宜提价，先看批价") == "oppose"
    assert claim_side("半托管毛利被关税吃掉", "关税影响有限，Temu 毛利还能守住") == "oppose"
    assert claim_side("半托管毛利被关税吃掉", "关税把半托管的毛利吃掉了") == "support"
    views = [
        {
            "article_id": 210,
            "author": "方伟",
            "stance": "positive",
            "judgment": "茅台长期仍好，但现阶段不宜提价",
            "quote": "现阶段不宜提价",
            "content_hash": "a",
            "date": "2024-01-01",
            "published_at": None,
        },
        {
            "article_id": 267,
            "author": "林文丰",
            "stance": "positive",
            "judgment": "飞天还能卖，只是现在不能提价",
            "quote": "现在不能提价",
            "content_hash": "b",
            "date": "2024-02-01",
            "published_at": None,
        },
        {
            "article_id": 11,
            "author": "甲",
            "stance": "positive",
            "judgment": "茅台仍有提价空间，批价撑得住",
            "quote": "茅台仍有提价空间",
            "content_hash": "c",
            "date": "2024-03-01",
            "published_at": None,
        },
    ]
    reply = (
        '{"theses": [{"title": "茅台仍有提价空间", "judgment": "茅台仍有提价空间",'
        ' "mechanism": "批价撑得住", "views": ["v210", "v267", "v11"]}]}'
    )
    parsed = parse_theses(reply, views, company="贵州茅台")
    assert parsed and set(parsed[0]["against"]) == {210, 267}
    assert parsed[0]["for"] == [11]
    assert parsed[0]["evidence"] and parsed[0]["sources"]


def test_reposts_count_once_and_overlapping_theses_merge():
    shared = "渠道反馈飞天动销平稳，批价也稳住了，这是同一篇转过来的"
    views = [
        {"article_id": 498, "author": "方伟", "stance": "positive", "judgment": shared, "quote": "",
         "content_hash": "a", "date": "2024-01-01", "published_at": None},
        {"article_id": 7457, "author": "方伟", "stance": "positive", "judgment": shared,
         "quote": "飞天动销平稳，批价也稳住了", "content_hash": "b", "date": "2024-01-02", "published_at": None},
        {"article_id": 3, "author": "乙", "stance": "negative", "judgment": "飞天动销并不平稳，批价松了",
         "quote": "飞天动销并不平稳", "content_hash": "c", "date": "2024-01-03", "published_at": None},
        {"article_id": 4, "author": "丙", "stance": "positive", "judgment": "飞天动销平稳，终端还在补货",
         "quote": "终端还在补货", "content_hash": "d", "date": "2024-01-04", "published_at": None},
    ]
    assert same_repost(views[0], views[1])
    reply = (
        '{"theses": ['
        '{"title": "飞天动销平稳", "judgment": "飞天动销平稳", "mechanism": "批价稳住",'
        ' "views": ["v498", "v7457", "v3", "v4"]},'
        '{"title": "飞天动销平稳", "judgment": "飞天动销平稳，批价稳住", "mechanism": "批价稳住",'
        ' "views": ["v7457", "v3", "v4"]}]}'
    )
    parsed = parse_theses(reply, views, company="贵州茅台")
    assert len(parsed) == 1
    assert 498 not in parsed[0]["sources"]
    assert 7457 in parsed[0]["sources"]
    assert 3 in parsed[0]["against"]

"""OCR 旁路、引用规范化、补抽缺口的纯逻辑、星球发帖人、论点的边和重叠。"""

import random
from pathlib import Path

from researchlib import theses as theses_mod
from researchlib.author_voice import assign_poster_role, is_guest
from researchlib.claims import claim_side, looks_like_list, plain_cite, quote_ok, same_repost
from researchlib.excerpt import view_windows
from researchlib.ocr import append_ocr, compose_ocr_text
from researchlib.theses import parse_theses
from researchlib.views import coerce_view_for_store, locate_quote, parse_views


class _Conn:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return [{"ord": 3, "body": "现阶段不宜提价，批价已经很高。"}]


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


def test_star_nickname_from_the_source_config_is_the_owner():
    """方伟在星球里的显示名是「方伟Rey」：来源配置的 poster_names 里有它，就不是读者帖。"""
    from researchlib.sources import load_sources

    config = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"
    zsxq = next(source for source in load_sources(config) if source.id == "fangwei-zsxq")
    assert "方伟Rey" in zsxq.poster_names
    assert assign_poster_role({"poster_name": "方伟Rey"}, zsxq.author, zsxq.poster_names)["poster_role"] == "owner"
    assert assign_poster_role({"poster_name": "方伟 Rey"}, zsxq.author, zsxq.poster_names)["poster_role"] == "owner"
    # 没配昵称时还是按名字认，读者照样是读者。
    assert is_guest(assign_poster_role({"poster_name": "方伟Rey"}, "方伟"))
    assert is_guest(assign_poster_role({"poster_name": "王小明"}, zsxq.author, zsxq.poster_names))


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


def _collapse_one_by_one(views: list[dict]) -> tuple[list[dict], int]:
    """转贴只留一条的规则本身：每条和已经留下的逐条比，并到最前面对得上的那条。另报后来的顶掉先留下的几次。"""

    def rank(item: dict) -> tuple[int, int]:
        return (1 if quote_ok(str(item.get("quote") or "")) else 0, -int(item["article_id"]))

    kept: list[dict] = []
    swapped = 0
    for view in views:
        twin = next((index for index, prev in enumerate(kept) if same_repost(prev, view)), None)
        if twin is None:
            kept.append(view)
        elif rank(view) > rank(kept[twin]):
            kept[twin] = view
            swapped += 1
    return kept, swapped


def _repost_cases(rng: random.Random, count: int) -> list[dict]:
    """互相包含、只差标点和全角、太短不算数、没有原句的都有；字表小，碰巧包含的也多。"""
    letters = "腾讯广告加载率视频号电商增长利润回购拼多多补贴"
    seen: list[str] = []

    def piece() -> str:
        roll = rng.random()
        if seen and roll < 0.35:
            base = rng.choice(seen)
            kind = rng.random()
            if kind < 0.3:
                return base
            if kind < 0.6:
                start = rng.randint(0, max(0, len(base) - 1))
                return base[start : rng.randint(start, len(base))]
            return rng.choice(letters) * rng.randint(0, 3) + base + "。" + rng.choice(letters) * rng.randint(0, 3)
        if roll < 0.45:
            return rng.choice(["", " ", "图片", "\n\n", "A B"])
        text = "".join(rng.choice(letters) for _ in range(rng.randint(1, 30)))
        if rng.random() < 0.3:
            text = text[: len(text) // 2] + "， " + text[len(text) // 2 :] + "１２％"
        return text

    views = []
    for _ in range(count):
        judgment, quote = piece(), piece()
        seen.extend([judgment, quote])
        if rng.random() < 0.05:
            quote = None
        views.append({"article_id": rng.randint(1, count * 2), "judgment": judgment, "quote": quote})
    return views


def test_collapse_reposts_gives_the_same_result_as_comparing_one_by_one():
    rng = random.Random(20261114)
    merged = replaced = 0
    for _ in range(300):
        views = _repost_cases(rng, rng.randint(0, 30))
        want, swapped = _collapse_one_by_one(views)
        got = theses_mod._collapse_reposts(views)
        assert [id(view) for view in got] == [id(view) for view in want]
        merged += len(views) - len(want)
        replaced += swapped
    # 这批样本里并掉的、后来的顶掉先留下的都要有，不然这条测试什么也没比。
    assert merged > 500 and replaced > 50


def test_collapse_reposts_compares_against_the_view_that_is_kept_now():
    """先留下的一条被转贴顶掉以后，后面的观点和现在留着的这条比，不和被顶掉的那条比。"""
    first = {"article_id": 10, "judgment": "腾讯视频号的广告加载率还很低有提升空间",
             "quote": "视频号广告加载率不到抖音的三分之一"}
    repost = {"article_id": 5, "judgment": "微信支付的费率这两年守住了没有再降",
              "quote": "方伟：视频号广告加载率不到抖音的三分之一，这是原话"}
    like_first = {"article_id": 20, "judgment": "我也认为腾讯视频号的广告加载率还很低有提升空间，别的不说",
                  "quote": "游戏版号恢复常态化发放"}
    like_repost = {"article_id": 30, "judgment": "一句话：微信支付的费率这两年守住了没有再降",
                   "quote": "小程序的交易额还在涨"}
    assert same_repost(first, repost) and same_repost(first, like_first)
    assert not same_repost(repost, like_first) and same_repost(repost, like_repost)
    views = [first, repost, like_first, like_repost]
    assert theses_mod._collapse_reposts(views) == [repost, like_first]
    assert _collapse_one_by_one(views) == ([repost, like_first], 1)


def test_collapse_reposts_does_not_compare_every_pair(monkeypatch):
    """一家公司上千条判断时逐条比是几十万对，公司页每次打开都要等十几秒；现在只核查得到的那几条。"""
    rng = random.Random(7)
    letters = [chr(code) for code in range(0x4E00, 0x4E00 + 1500)]

    def sentence(low: int, high: int) -> str:
        return "".join(rng.choice(letters) for _ in range(rng.randint(low, high)))

    views = [
        {"article_id": index + 1, "judgment": sentence(30, 90), "quote": sentence(20, 120)} for index in range(1500)
    ]
    views.append({"article_id": 9001, "judgment": "又一遍：" + views[40]["judgment"], "quote": ""})
    views.append({"article_id": 9002, "judgment": sentence(30, 90), "quote": views[700]["quote"][3:40]})
    compared = []
    real = theses_mod.same_key
    monkeypatch.setattr(theses_mod, "same_key", lambda left, right: compared.append(1) or real(left, right))
    kept = theses_mod._collapse_reposts(views)
    assert len(kept) == 1500 and views[40] in kept and views[700] in kept
    assert 2 <= len(compared) < 20


def test_coerce_view_none_is_not_about():
    got = coerce_view_for_store(None)
    assert got["about"] is False
    assert got["judgment"] == ""


def test_coerce_view_empty_dict_is_not_about():
    # 旧路径 view or {} 后 about 缺省 true —— 必须拦住
    got = coerce_view_for_store({})
    assert got["about"] is False
    assert got["judgment"] == ""


def test_coerce_view_about_true_without_judgment_is_not_about():
    got = coerce_view_for_store({"about": True, "judgment": "  ", "stance": "neutral"})
    assert got["about"] is False
    assert got["judgment"] == ""


def test_coerce_view_keeps_real_judgment():
    got = coerce_view_for_store(
        {"about": True, "judgment": "顺丰份额稳定", "stance": "neutral", "mechanism": "时效"}
    )
    assert got["about"] is True
    assert got["judgment"] == "顺丰份额稳定"
    assert got["mechanism"] == "时效"


def test_views_command_rejects_worker_counts_out_of_range():
    import pytest

    from researchlib.llm_batch import MAX_WORKERS
    from researchlib.views import main

    for count in ("0", str(MAX_WORKERS + 1)):
        with pytest.raises(SystemExit) as stopped:
            main(["--workers", count])
        assert "--workers" in str(stopped.value)

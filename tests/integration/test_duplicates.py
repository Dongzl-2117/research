"""同一篇从两条路进来时只留一个主版本。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.duplicates import fingerprint, rebuild_duplicates, same_article
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")

BODY = """
## 广告

腾讯的广告加载率还在提升，视频号是新的广告库存，商家的投放在往这里挪。

腾讯把小程序和视频号接起来以后，交易闭环更短，广告主愿意出的价格也更高。

## 风险

如果视频号的用户时长停止增长，这个判断就要重新看，广告库存的增量会明显放缓。

腾讯的游戏业务这季度没有新的大作，收入主要靠老游戏的长线运营撑着。

金融科技的支付流水跟着消费走，今年的增速比广告慢，利润率倒是在往上走。

云业务砍掉了低毛利的项目以后，收入规模没怎么涨，但亏损收窄得很快。
"""

FOOTER = "以上内容只代表个人观点，不构成任何投资建议，请读者自行判断。\n\n欢迎转发给更多关心长期投资的朋友阅读。"


def _doc(external_id, title, body, day=12):
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2025, 3, day, 9, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={},
        image_refs=[],
    )


def _store(conn, settings, source_id, docs):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    return store_documents(
        conn,
        source=sources[source_id],
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )


def _count(conn, table, article_id):
    return conn.execute(f"SELECT count(*) AS n FROM {table} WHERE article_id = %s", (article_id,)).fetchone()["n"]


def _row(conn, external_id):
    return conn.execute(
        "SELECT id, duplicate_of FROM articles WHERE external_id = %s", (external_id,)
    ).fetchone()


def test_same_article_from_two_paths_keeps_one(conn, settings):
    _store(conn, settings, "fangwei-mp", [_doc("mp/2025-03-12.md", "腾讯的广告库存", BODY)])
    # 同一篇又从 AIHOT 导进来：外部 id 不同、开头多了一行归档头。带归档头的略长，当主版本。
    report = _store(
        conn,
        settings,
        "fangwei-mp",
        [_doc("aihot:991", "腾讯的广告库存", "原创 方伟 方伟看十年 2025年3月12日 09:00 上海\n" + BODY)],
    )
    first, second = _row(conn, "mp/2025-03-12.md"), _row(conn, "aihot:991")
    assert report.duplicates == 1 or first["duplicate_of"] == second["id"] or second["duplicate_of"] == first["id"]
    assert first["duplicate_of"] == second["id"] and second["duplicate_of"] is None
    assert _count(conn, "article_chunks", first["id"]) == 0
    assert _count(conn, "article_mentions", first["id"]) == 0
    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        assert f'href="/articles/{second["id"]}"' in home and f'href="/articles/{first["id"]}"' not in home
        found = client.get("/search", params={"q": "广告库存"}).text
        assert f'href="/articles/{second["id"]}' in found and f'href="/articles/{first["id"]}' not in found
        page = client.get(f"/articles/{first['id']}")
        assert page.status_code == 200 and "同一篇也在" in page.text
        assert "同一篇也在" in client.get(f"/articles/{second['id']}").text
        tencent = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
        company = client.get(f"/companies/{tencent}").text
        assert company.count(f'href="/articles/{second["id"]}') >= 1
        assert f'href="/articles/{first["id"]}' not in company


def test_repost_on_another_source_of_the_same_author(conn, settings):
    _store(conn, settings, "fangwei-mp", [_doc("mp/a.md", "腾讯的广告库存", BODY)])
    _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:1", "星球转发", BODY + "\n\n星球里补一句：这篇先发在公众号。")])
    # 星球那版加了点评，正文更长，当主版本。
    assert _row(conn, "mp/a.md")["duplicate_of"] == _row(conn, "zsxq:1")["id"]
    assert _row(conn, "zsxq:1")["duplicate_of"] is None
    # 不同作者写了相同的话不算同一篇。
    _store(conn, settings, "chengfengpolang", [_doc("cf:1", "转述", BODY)])
    assert _row(conn, "cf:1")["duplicate_of"] is None


def test_shared_footer_does_not_make_different_posts_the_same(conn, settings):
    posts = [
        _doc(
            f"zsxq:{index}",
            f"短帖 {index}",
            f"今天第 {index} 条：拼多多的商家佣金率又调了一次，第{index}季度的变化值得记下来。\n\n{FOOTER}",
            day=index,
        )
        for index in range(1, 9)
    ]
    _store(conn, settings, "fangwei-zsxq", posts)
    assert conn.execute("SELECT count(*) AS n FROM articles WHERE duplicate_of IS NOT NULL").fetchone()["n"] == 0


def test_short_quote_is_not_a_duplicate():
    long = fingerprint(BODY)
    quote = fingerprint("腾讯的广告加载率还在提升，视频号是新的广告库存，商家的投放在往这里挪。")
    assert not same_article(long, quote)
    assert same_article(fingerprint(BODY), fingerprint("原创 方伟\n" + BODY))


def test_rebuild_finds_old_duplicates_and_prefers_the_longer_copy(conn, settings):
    _store(conn, settings, "fangwei-mp", [_doc("mp/x.md", "腾讯的广告库存", BODY)])
    extra = (
        "\n\n补充一段更长的说明：视频号广告的填充率还有空间，商家端的工具也在补齐。"
        "\n\n直播电商的佣金和广告是两条线，腾讯在两边都还没有用足，后面要看商家的复购。"
        "\n\n小游戏的买量也在走视频号，这部分收入在财报里放在营销服务下面，口径要注意。"
    )
    _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:x", "转发", BODY + extra)])
    # 模拟去重上线之前入库的数据：清掉标记和指纹。
    conn.execute("UPDATE articles SET duplicate_of = NULL, fingerprint_of = NULL")
    conn.execute("DELETE FROM article_fingerprints")
    conn.commit()
    result = rebuild_duplicates(conn)
    assert result["duplicates"] == 1
    # 星球那份更长，当主版本；公众号那份指向它，块和提及都删掉。
    longer, shorter = _row(conn, "zsxq:x"), _row(conn, "mp/x.md")
    assert longer["duplicate_of"] is None and shorter["duplicate_of"] == longer["id"]
    assert _count(conn, "article_chunks", longer["id"]) > 0
    assert _count(conn, "article_mentions", longer["id"]) > 0


def test_duplicate_that_changes_is_released(conn, settings):
    _store(conn, settings, "fangwei-mp", [_doc("mp/y.md", "腾讯的广告库存", BODY)])
    _store(conn, settings, "fangwei-mp", [_doc("aihot:7", "腾讯的广告库存", BODY)])
    assert _row(conn, "aihot:7")["duplicate_of"] is not None
    other = "\n\n".join(
        f"第{index}段：美团的配送成本和骑手补贴在变，这一段写的是完全不同的一件事，和广告无关。" for index in range(6)
    )
    _store(conn, settings, "fangwei-mp", [_doc("aihot:7", "美团的配送", other)])
    released = _row(conn, "aihot:7")
    assert released["duplicate_of"] is None
    assert _count(conn, "article_chunks", released["id"]) > 0


def test_separate_undoes_a_wrong_match(conn, settings):
    from researchlib.duplicates import separate
    from researchlib.mentions import CompanyMatcher, load_company_rows

    _store(conn, settings, "fangwei-mp", [_doc("mp/z.md", "腾讯的广告库存", BODY)])
    _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:z", "星球", BODY)])
    copy = _row(conn, "zsxq:z")
    assert copy["duplicate_of"] is not None
    assert separate(conn, copy["id"], CompanyMatcher(load_company_rows(conn)))
    assert _row(conn, "zsxq:z")["duplicate_of"] is None
    assert _count(conn, "article_chunks", copy["id"]) > 0
    # 以后再导入、再全库认一遍，都不会把它认回重复。
    _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:z", "星球", BODY + "\n\n补一句。")])
    rebuild_duplicates(conn)
    assert _row(conn, "zsxq:z")["duplicate_of"] is None


def test_short_repost_with_an_extra_line_is_found_at_ingest(conn, settings):
    lines = [
        "拼多多的主站在这个季度里继续压低佣金率，商家端的工具也在补齐。",
        "Temu 在美国的半托管模式又调整了一次，物流成本是这次的关键。",
        "拼多多要靠组织效率撑住低价，这一点和别的平台不一样。",
    ]
    _store(conn, settings, "fangwei-mp", [_doc("mp/short.md", "短文", "\n\n".join(lines))])
    repost = "\n\n".join(lines) + "\n\n星球里补一句：这篇先发在公众号。"
    _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:short", "转发", repost)])
    assert _row(conn, "mp/short.md")["duplicate_of"] == _row(conn, "zsxq:short")["id"]
    assert _row(conn, "zsxq:short")["duplicate_of"] is None


def test_prefers_longer_commentary_over_more_images(conn, settings):
    """星球转发加了点评的那版即使图少，也要当主版本，不能被图多的公众号稿藏掉。"""
    mp = _store(conn, settings, "fangwei-mp", [_doc("mp/img.md", "腾讯的广告库存", BODY)])
    assert mp.inserted == 1
    mp_id = _row(conn, "mp/img.md")["id"]
    # 给公众号那份挂上多张图（模拟图多）。
    for index in range(5):
        conn.execute(
            "INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)"
            " VALUES (%s, %s, %s, %s, %s)",
            (mp_id, "fangwei-mp", f"img/{index}.jpg", f"images/fake-{index}.jpg", f"hash-{index}"),
        )
    conn.commit()
    commentary = BODY + "\n\n星球补评：这一段广告库存的判断，后面要对照视频号时长再看一眼。"
    _store(conn, settings, "fangwei-zsxq", [_doc("zsxq/img.md", "转发加评", commentary)])
    zsxq, mp_row = _row(conn, "zsxq/img.md"), _row(conn, "mp/img.md")
    assert zsxq["duplicate_of"] is None and mp_row["duplicate_of"] == zsxq["id"]


def test_shared_cta_footer_does_not_merge_different_posts(conn, settings):
    """两篇不同主题的正文，末尾都挂同一段很长的社群广告，不能并成一篇。"""
    cta = "\n\n".join(
        [
            "为什么值得加入",
            "1月份的腾讯都没买的话，这么多年的互联网白干了。",
            "中国的股市真的魔幻，微信一个送礼物的功能，微盟一度涨了50%。",
            "即便无人看好，依然加仓拼多多，静待花开。",
            "1.2 万亿的美团真的被高估了吗？泡沫到底有多大？",
            "1.4万亿的小米是不是下一个特斯拉？这个问题要分开看硬件和互联网。",
            "京东外卖别说突破100万单，突破500万单也没用。",
            "快手电商已死，只是余温尚存，别再按旧口径估。",
            "微信会员群只适合有价值投资经验的朋友加入，888/年，送365/年星球会员。",
        ]
    )
    xiaomi = "\n\n".join(
        [
            "群友问小米到底是制造业还是互联网。",
            "小米接近一半利润来自互联网增值服务，毛利率七十多。",
            "谁需要一个全是广告的廉价手机呢，品牌要靠体验。",
            "小米的模式跟 Costco 的会员费模式学习了很多。",
        ]
    ) + cta
    pdd = "\n\n".join(
        [
            "资本永远寻求更高的投资回报率，增长是企业永恒的命题。",
            "美团与京东之战，抽象来看是一次没能全球化的内卷。",
            "中国互联网最专注的公司是拼多多，国内外只有一个业务。",
            "阿里京东业务线太多，协同性很差，美其名曰多元化布局。",
        ]
    ) + cta
    _store(conn, settings, "fangwei-mp", [_doc("mp/xiaomi.md", "小米是制造业还是互联网", xiaomi)])
    _store(conn, settings, "fangwei-mp", [_doc("mp/pdd.md", "拼多多的专注", pdd)])
    assert _row(conn, "mp/xiaomi.md")["duplicate_of"] is None
    assert _row(conn, "mp/pdd.md")["duplicate_of"] is None

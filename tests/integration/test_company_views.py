"""公司页的判断：抽作者判断，风险等只在原文谈到时才有，原句逐字核对；页面分概览、判断、文章三页。"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import views as views_mod
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.llm import LLMError
from researchlib.seed import load_sample
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _sample(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)


def _tencent(conn):
    return conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '腾讯'").fetchone()


def _article(conn, external_id):
    return conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id FROM articles WHERE external_id = %s",
        (external_id,),
    ).fetchone()


def _reply(stance, **extra):
    view = {
        "judgment": "腾讯的广告还有提价空间，视频号是新的库存。",
        "mechanism": "视频号把内容和交易接起来，广告主愿意出更高的价。",
        "stance": stance,
        "risks": [],
        "falsifiers": [],
        "metrics": [],
        "quote": "腾讯视频号和腾讯金融科技各写一层",
    }
    view.update(extra)
    return json.dumps({"腾讯": view, "美团": None}, ensure_ascii=False)


def test_parse_views_keeps_only_named_companies_and_optional_fields():
    text = '好的 {"腾讯": {"judgment": "看好", "stance": "两面"}, "别的": {"judgment": "x"}}'
    found = views_mod.parse_views(text, {"腾讯": 1, "拼多多": 2})
    assert found[2] is None
    assert found[1]["stance"] == "mixed" and found[1]["risks"] == [] and found[1]["falsifiers"] == []
    with pytest.raises(LLMError):
        views_mod.parse_views("没有 JSON", {"腾讯": 1})


def test_timeline_on_company_page(conn, settings, monkeypatch):
    _sample(conn, settings)
    company = _tencent(conn)
    ads = _article(conn, "sample:tencent-ads")
    replies = iter(
        [
            _reply("正面"),
            _reply("负面", risks=["视频号时长停止增长"], falsifiers=["广告加载率见顶"], quote="这句原文里没有"),
        ]
    )
    monkeypatch.setattr(views_mod, "chat", lambda _s, _m, **_k: (next(replies), "grok-test"))
    assert views_mod.extract_article(conn, settings, ads, "方伟", [company]) == 1
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    later = ParsedDoc(
        external_id="mp/tencent-later",
        title="再看腾讯",
        published_at=datetime(2025, 8, 1, 9, 0, tzinfo=SHANGHAI),
        body_md="## 广告\n\n腾讯的广告加载率接近上限。腾讯视频号的时长增长放缓。腾讯要靠别的业务了。",
        raw_meta={},
        image_refs=[],
    )
    store_documents(conn, source=sources["fangwei-mp"], docs=[later], data_dir=settings.data_dir,
                    archive_files=1, images_in_archive=0, errors=[])
    second = _article(conn, "mp/tencent-later")
    views_mod.extract_article(conn, settings, second, "方伟", [company])
    saved = conn.execute(
        "SELECT judgment, quote, chunk_ord, stance FROM company_views WHERE company_id = %s ORDER BY article_id",
        (company["id"],),
    ).fetchall()
    # 第一条原句在原文里，留下并定位到段落；第二条对不上，丢掉。
    assert saved[0]["quote"] and saved[0]["chunk_ord"] is not None
    assert saved[1]["quote"] == "" and saved[1]["stance"] == "negative"
    with TestClient(create_app(settings)) as client:
        base = f"/companies/{company['id']}"
        # 概览：这位作者现在怎么看，最近的判断不用翻页就在。三页的标签上写着判断有几条。
        overview = client.get(base).text
        assert "现在怎么看" in overview and "腾讯的广告还有提价空间" in overview
        assert 'id="views"' not in overview and 'class="company-articles"' not in overview
        assert f'href="{base}?tab=views"' in overview and f'href="{base}?tab=articles"' in overview
        # 判断一页：一条一行，点开有理由和推翻条件，「原文」落到那一段。
        page = client.get(base, params={"tab": "views"}).text
        assert 'id="views"' in page and "腾讯的广告还有提价空间" in page
        assert "推翻条件" in page and "广告加载率见顶" in page
        assert f'href="/articles/{ads["id"]}?chunk={saved[0]["chunk_ord"]}"' in page
        # 11-11 改版去掉了每条上的「看好 → 看淡」和「风险与反方」筛选；旧链接照样开到判断这一页，只是不再筛。
        assert "看好 → 看淡" not in page
        risk = client.get(base, params={"views": "risk"}).text
        assert 'id="views"' in risk and "腾讯视频号和腾讯金融科技各写一层" in risk
        # 按态度筛：只看看淡的那条。
        negative = client.get(base, params={"tab": "views", "stance": "negative"}).text
        section = negative.split('id="views"', 1)[1]
        assert "视频号时长停止增长" in section and "腾讯视频号和腾讯金融科技各写一层" not in section
        # 只带作者的旧链接也到判断这一页；文章一页还是原来那些。
        assert 'id="views"' in client.get(base, params={"author": "方伟"}).text
        articles = client.get(base, params={"tab": "articles"}).text
        assert 'class="company-articles"' in articles and "谁在谈" in articles and 'id="views"' not in articles
        # 接口上的「风险与反方」筛选还在。
        data = client.get(f"/api/v1/companies/{company['id']}/views", params={"views": "risk"}).json()
        assert [item["stance"] for item in data["views"]] == ["negative"]
        # 公司列表：这家有几条判断、最近一条是什么。
        listing = client.get("/companies").text
        row = listing.split(f'href="{base}"', 1)[1].split("</li>", 1)[0]
        assert "腾讯的广告还有提价空间" in row and 'class="stance-chart"' in row
        # 不在持仓里、但示例库深入谈到的公司也会整理观点。还没整理出来的只有文章这一页，写明还差几篇。
        meituan = conn.execute("SELECT id FROM companies WHERE name_zh = '美团'").fetchone()["id"]
        meituan_page = client.get(f"/companies/{meituan}").text
        assert 'class="company-articles"' in meituan_page and "没整理出判断" in meituan_page
        assert 'class="tabs"' not in meituan_page


def test_pending_articles_cover_all_tracked_companies_once(conn, settings):
    _sample(conn, settings)
    tracked = views_mod.tracked_companies(conn, ["腾讯", "贵州茅台", "拼多多"])
    ids = [row["id"] for row in tracked]
    sources = [source.id for source in load_sources(settings.sources_path) if source.enabled]
    rows = views_mod.pending_articles(conn, ids, sources, 50)
    assert rows and len({row["id"] for row in rows}) == len(rows)
    first = rows[0]
    pending = views_mod.pending_companies(conn, first["id"], ids)
    assert {item["id"] for item in pending} <= set(ids)
    assert views_mod.count_pending(conn, _tencent(conn)["id"], sources) >= 1


def test_only_flips_between_bullish_and_bearish_count_as_changes(conn, settings):
    """看好 → 两面 → 看好 不算态度变化；看好 → 两面 → 看淡 只在看淡那篇标一次「看好 → 看淡」。"""
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    stances = ["positive", "mixed", "positive", "neutral", "mixed", "negative", "mixed", "negative"]
    docs = [
        ParsedDoc(
            external_id=f"mp/tencent-{index}",
            title=f"腾讯第 {index} 篇",
            published_at=datetime(2025, 1 + index, 1, 9, 0, tzinfo=SHANGHAI),
            body_md=f"腾讯的广告第 {index} 次。腾讯的游戏第 {index} 次。腾讯的云第 {index} 次。",
            raw_meta={},
            image_refs=[],
        )
        for index in range(len(stances))
    ]
    store_documents(conn, source=sources["fangwei-mp"], docs=docs, data_dir=settings.data_dir,
                    archive_files=len(docs), images_in_archive=0, errors=[])
    company = _tencent(conn)
    for index, stance in enumerate(stances):
        row = _article(conn, f"mp/tencent-{index}")
        conn.execute(
            """
            INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (row["id"], company["id"], row["content_hash"], views_mod.PROMPT_VERSION, f"第 {index} 篇的判断", stance),
        )
    conn.commit()
    items = views_mod.timeline(conn, company["id"], sources, settings)
    changed = [(item["title"], item["previous_label"], item["stance_label"]) for item in items if item["changed"]]
    assert changed == [("腾讯第 5 篇", "看好", "看淡")]
    assert [item["title"] for item in views_mod.filter_timeline(items, "change")] == ["腾讯第 5 篇"]


def test_changes_require_ordered_publications(conn, settings):
    company_id = _tencent(conn)["id"]
    rows = [
        # 入库先后不代表发表时间。无日期判断也不能作第一条有日期判断的前态。
        ("unknown-up", None, "positive", False),
        ("unknown-down", None, "negative", False),
        ("first", "2025-06-01 10:00+08", "positive", True),
        ("later", "2025-06-01 11:00+08", "negative", True),
        # 同时刻互相矛盾，不能拿 id 排出先后，下一天也没有唯一的前态。
        ("tie-up", "2025-06-02 10:00+08", "positive", True),
        ("tie-down", "2025-06-02 10:00+08", "negative", True),
        ("after-tie", "2025-06-03 00:00+08", "negative", False),
        ("next-day", "2025-06-04 00:00+08", "positive", False),
        # 同一天只有日期的一条与有钟点的一条，无法断定谁在先。
        ("no-clock", "2025-06-05 00:00+08", "negative", False),
        ("same-day", "2025-06-05 11:00+08", "positive", True),
    ]
    for key, published, stance, clock in rows:
        article_id = conn.execute(
            """INSERT INTO articles(source_id,external_id,title,body_text,body_md,content_hash,published_at,raw_meta)
               VALUES('fangwei-mp',%s,%s,'样本正文','样本正文',%s,%s,%s) RETURNING id""",
            (key, key, key, published, Jsonb({"time_known": clock})),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO company_views(article_id,company_id,content_hash,prompt_version,judgment,stance)
               VALUES(%s,%s,%s,2,'对毛利的判断',%s)""", (article_id, company_id, key, stance),
        )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        url = f"/api/v1/companies/{company_id}/views"
        all_views = client.get(url).json()
        assert all_views["total"] == len(rows)
        changes = client.get(url, params={"views": "change"}).json()["views"]
        assert [(v["title"], v["previous_label"]) for v in changes] == [
            ("next-day", "看淡"), ("later", "看好"),
        ]
        tool = client.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_company_views", "arguments": {"id": company_id, "views": "change"}},
        }).json()["result"]["structuredContent"]
        assert tool["total"] == 2 and tool["views"] == changes


def test_ocr_changed_picks_only_views_drawn_from_other_image_text(conn, settings):
    """research-views --ocr-changed：只挑已经抽过观点、抽的时候的图片字和现在不一样的文章。
    还没抽过的、图片字没变的不挑。挑中的这篇照常把该整理的公司一次问完。"""
    company = _tencent(conn)
    sources = [source.id for source in load_sources(settings.sources_path) if source.enabled]
    ids = {}
    for external_id, ocr_hash in (("reread", "newhash"), ("never", "h1"), ("same", "samehash")):
        ids[external_id] = conn.execute(
            """
            INSERT INTO articles (
              source_id, external_id, content_hash, title, published_at, body_md, body_text, ocr_hash, ocr_text
            ) VALUES ('fangwei-zsxq', %s, %s, %s, '2026-10-01 12:00+08', '图片', '图片', %s, '腾讯视频号的广告还能涨价')
            RETURNING id
            """,
            (external_id, f"hash-{external_id}", f"腾讯截图 {external_id}", ocr_hash),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO article_mentions (article_id, company_id, depth, hit_count) VALUES (%s, %s, 'passing', 1)",
            (ids[external_id], company["id"]),
        )
    for external_id, ocr_hash in (("reread", "oldhash"), ("same", "samehash")):
        conn.execute(
            """
            INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance, ocr_hash)
            VALUES (%s, %s, %s, %s, '视频号广告还有提价空间', 'positive', %s)
            """,
            (ids[external_id], company["id"], f"hash-{external_id}", views_mod.PROMPT_VERSION, ocr_hash),
        )
    conn.commit()
    queued = {row["id"] for row in views_mod.pending_articles(conn, [company["id"]], sources, 50)}
    assert queued == {ids["reread"], ids["never"]}
    picked = views_mod.pending_articles(conn, [company["id"]], sources, 50, ocr_changed=True)
    assert [row["id"] for row in picked] == [ids["reread"]]
    assert [item["id"] for item in views_mod.pending_companies(conn, ids["reread"])] == [company["id"]]


def test_company_page_hides_wrong_financial_quote_and_reads_old_secondary_angles(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-mp")
    body = "腾讯的净利润同比变化-1.5%。腾讯的广告渠道库存明显下降。"
    store_documents(conn, source=source,
                    docs=[ParsedDoc("review-angles", "腾讯业务变化", datetime(2026, 1, 1, tzinfo=SHANGHAI),
                                    body, {}, [])], data_dir=settings.data_dir, archive_files=1,
                    images_in_archive=0, errors=[])
    company = _tencent(conn)["id"]
    row = _article(conn, "review-angles")
    angles = [{"judgment": "广告库存改善", "quote": "腾讯的广告渠道库存明显下降。", "stance": "positive"},
              {"judgment": "第三项仍待核实", "quote": "毛利率增加99%。", "stance": "neutral"}]
    conn.execute(
        "INSERT INTO company_views(article_id, company_id, content_hash, prompt_version, judgment, stance,"
        " quote, chunk_ord, angles) VALUES (%s, %s, %s, 2, '本期净利发生变化', 'mixed', %s, 0, %s::jsonb)",
        (row["id"], company, row["content_hash"], "腾讯的净利润同比变化15%。", json.dumps(angles)))
    conn.commit()
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/companies/{company}", params={"tab": "views"}).text
        assert "同篇其他判断" in page and "广告库存改善" in page
        assert "毛利率增加99%" not in page
        item = client.get(f"/api/v1/companies/{company}/views").json()["views"][0]
        # 公司页不回读全文：主判断的旧原句对不上这一块只撤链接（入库时已核过），新入库的由 locate_quote 拦住。
        assert item["chunk"] is None
        assert item["angles"][0]["quote"] == angles[0]["quote"] and item["angles"][1]["quote"] == ""


def test_peer_author_survives_more_than_sixty_closer_same_author_articles(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    _sample(conn, settings)
    company = _tencent(conn)["id"]
    base = _article(conn, "sample:tencent-ads")
    conn.execute("""
        WITH added AS (
            INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)
            SELECT CASE WHEN i < 62 THEN 'fangwei-mp' ELSE 'gudujinhuati' END,
                'peer-' || i, 'hash-' || i, '同公司归档 ' || i,
                %s::timestamptz + i * interval '1 minute', '合成正文', '合成正文'
            FROM generate_series(1,62) i RETURNING id, content_hash)
        INSERT INTO company_views(article_id, company_id, content_hash, prompt_version, judgment, stance)
        SELECT id, %s, content_hash, 2, '渠道需求放缓', 'negative' FROM added
        """, (base["published_at"], company))
    peers = views_mod.same_period(conn, base["id"], None, sources, settings, limit=1)
    assert len(peers) == 1 and peers[0]["author"] == sources["gudujinhuati"].author

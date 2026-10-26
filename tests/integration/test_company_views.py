"""公司页的观点时间线：抽作者判断，风险等只在原文谈到时才有，原句逐字核对，态度变了标出来。"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from owner_fixtures import load_browsing_sample as load_sample

from researchlib import views as views_mod
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.llm import LLMError
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
        page = client.get(f"/companies/{company['id']}").text
        assert 'id="views"' in page and "腾讯的广告还有提价空间" in page
        assert "推翻条件" in page and "广告加载率见顶" in page
        assert "看好 → 看淡" in page
        assert f'href="/articles/{ads["id"]}?chunk={saved[0]["chunk_ord"]}"' in page
        risk = client.get(f"/companies/{company['id']}", params={"views": "risk"}).text
        section = risk.split('id="views"', 1)[1].split('class="company-articles"', 1)[0]
        assert "视频号时长停止增长" in section and "腾讯视频号和腾讯金融科技各写一层" not in section
        # 不在持仓里、但示例库深入谈到的公司也会整理观点。
        meituan = conn.execute("SELECT id FROM companies WHERE name_zh = '美团'").fetchone()["id"]
        meituan_page = client.get(f"/companies/{meituan}").text
        assert 'id="views"' in meituan_page
        assert conn.execute("SELECT count(*) AS n FROM company_views WHERE company_id = %s",
                            (meituan,)).fetchone()["n"] == 0


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

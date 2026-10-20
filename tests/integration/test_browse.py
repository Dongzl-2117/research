from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.backfill import backfill_published_at
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

UTC = ZoneInfo("UTC")


def _source(settings, source_id):
    return next(source for source in load_sources(settings.sources_path) if source.id == source_id)


def _doc(external_id, title, when, body, source_hint=""):
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=when,
        body_md=body,
        raw_meta={"format": "markdown", "filename": source_hint or f"{external_id}.md"},
        image_refs=[],
    )


def test_home_pages_by_shanghai_day(conn, settings):
    fangwei = _source(settings, "fangwei-mp")
    other = _source(settings, "chengfengpolang")
    store_documents(
        conn,
        source=fangwei,
        docs=[
            _doc("late", "边界十点", datetime(2024, 3, 13, 2, 0, tzinfo=UTC), "十点这篇。"),
            _doc("early", "边界凌晨", datetime(2024, 3, 12, 16, 5, tzinfo=UTC), "凌晨这篇。"),
            _doc("prev", "前一天", datetime(2024, 3, 12, 15, 0, tzinfo=UTC), "前一天这篇。"),
            _doc("none", "没有日期", None, "日期空着。"),
        ],
        data_dir=settings.data_dir,
        archive_files=4,
        images_in_archive=0,
        errors=[],
    )
    store_documents(
        conn,
        source=other,
        docs=[_doc("other-day", "别的来源", datetime(2024, 3, 13, 3, 0, tzinfo=UTC), "另一处。")],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    app = create_app(replace(settings, days_per_page=1))
    with TestClient(app) as client:
        first = client.get("/")
        assert first.status_code == 200
        text = first.text
        # 页面上不再写怎么排序这类说明。
        assert "按日期从新到旧" not in text
        assert text.find("3月13日") < text.find("边界十点") < text.find("边界凌晨")
        assert text.find("10:00") < text.find("00:05")
        assert "前一天" not in text
        assert "没有日期" not in text
        assert "2024年" in text
        assert "星期三" in text
        second = client.get("/", params={"page": 2})
        assert "前一天" in second.text
        assert "23:00" in second.text
        assert "边界十点" not in second.text
        third = client.get("/", params={"page": 3})
        assert "日期不详" in third.text
        assert "没有日期" in third.text
        filtered = client.get("/", params={"source": "fangwei-mp"})
        assert "边界十点" in filtered.text
        assert "别的来源" not in filtered.text
        assert "只看这个来源" not in filtered.text
        assert client.get("/authors/林文丰").status_code == 200
        assert client.get("/authors/没有这个人").status_code == 404


def test_search_ranks_title_above_body_and_matches_author(conn, settings):
    source = _source(settings, "fangwei-mp")
    store_documents(
        conn,
        source=source,
        docs=[
            _doc("title-hit", "腾讯专文", datetime(2024, 1, 2, tzinfo=SHANGHAI), "这篇正文不写那个公司名。"),
            _doc("body-hit", "别的题目", datetime(2024, 6, 1, tzinfo=SHANGHAI), "只在正文里写到腾讯一次。"),
            _doc("body-only", "冷门词", datetime(2024, 6, 2, tzinfo=SHANGHAI), "正文里有一个独有词条甲乙丙。"),
        ],
        data_dir=settings.data_dir,
        archive_files=3,
        images_in_archive=0,
        errors=[],
    )
    app = create_app(settings)
    with TestClient(app) as client:
        ranked = client.get("/search", params={"q": "腾讯"})
        assert ranked.status_code == 200
        assert "<mark>腾讯</mark>专文" in ranked.text
        assert ranked.text.find("专文") < ranked.text.find("别的题目")
        assert 'class="pills search-pills"' in ranked.text
        body = client.get("/search", params={"q": "独有词条甲乙丙"})
        assert "冷门词" in body.text
        author = client.get("/search", params={"q": "方伟"})
        assert "冷门词" in author.text
        assert "方伟</span><strong>" in author.text
        missing = client.get("/search", params={"q": "绝无此词xyz"})
        assert "没有找到" in missing.text
        assert "换个词" not in missing.text


def test_backfill_fills_null_dates_from_stored_text(conn, settings):
    source = _source(settings, "fangwei-mp")
    store_documents(
        conn,
        source=source,
        docs=[
            ParsedDoc(
                external_id="bf-front",
                title="有前言日期",
                published_at=None,
                body_md="---\ndate: 2024年5月1日 21:05\n---\n正文\n",
                raw_meta={"format": "markdown", "filename": "plain.md"},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="bf-name",
                title="有文件名日期",
                published_at=None,
                body_md="没有前言。\n",
                raw_meta={"format": "markdown", "filename": "2023-12-01-note.md"},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="bf-none",
                title="没有日期",
                published_at=None,
                body_md="完全没有日期。\n",
                raw_meta={"format": "markdown", "filename": "plain.md"},
                image_refs=[],
            ),
        ],
        data_dir=settings.data_dir,
        archive_files=3,
        images_in_archive=0,
        errors=[],
    )
    stats = backfill_published_at(conn)
    assert stats == {"scanned": 3, "updated": 2, "remaining": 1}
    rows = {
        row["external_id"]: row
        for row in conn.execute("SELECT external_id, published_at, raw_meta FROM articles").fetchall()
    }
    front = rows["bf-front"]["published_at"].astimezone(SHANGHAI)
    assert (front.month, front.day, front.hour, front.minute) == (5, 1, 21, 5)
    assert rows["bf-front"]["raw_meta"]["date_source"] == "frontmatter"
    named = rows["bf-name"]["published_at"].astimezone(SHANGHAI)
    assert (named.year, named.month, named.day) == (2023, 12, 1)
    assert rows["bf-none"]["published_at"] is None


def test_day_page_shows_real_clock_and_summary(conn, settings):
    source = _source(settings, "fangwei-zsxq")
    talk = _doc(
        "talk-1329",
        "方伟知识星球短帖",
        datetime(2024, 10, 19, tzinfo=SHANGHAI),
        "这篇短帖在讲宁德时代的产能。宁德时代的客户。宁德时代的价格。\n\n比如腾讯当年、腾讯的做法、腾讯也这么干过。\n",
    )
    talk.raw_meta = {
        "format": "markdown",
        "filename": "talk.md",
        "date_source": "frontmatter",
        "time_known": False,
        "create_time": "2024-10-19T05:29:00+00:00",
    }
    midnight = _doc(
        "real-midnight",
        "正好午夜",
        datetime(2024, 10, 19, tzinfo=SHANGHAI),
        "这一篇的时间就是零点。\n",
    )
    midnight.raw_meta = {
        "format": "markdown",
        "filename": "midnight.md",
        "date_source": "meta",
        "time_known": True,
        "create_time": "2024-10-19T00:00:00+08:00",
    }
    store_documents(
        conn,
        source=source,
        docs=[talk, midnight],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    tencent = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
    app = create_app(settings)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "13:29" in home.text
        assert "00:00" in home.text
        assert "AI 整合" not in home.text
        assert 'href="/days/2024-10-19"' in home.text
        # 卡片上不挂一带而过的公司标签（首页顶部的持仓近况会链到腾讯，那不算卡片标签）。
        assert f'class="tag depth-passing" href="/companies/{tencent}"' not in home.text
        assert f'href="/companies/{tencent}">腾讯</a></li>' not in home.text
        day = client.get("/days/2024-10-19")
        assert day.status_code == 200
        assert "日汇总" in day.text
        assert "方伟：" in day.text
        assert "方伟知识星球短帖" in day.text
        assert "13:29" in day.text
        assert "AI 整合" not in day.text
        assert client.get("/days/not-a-day").status_code == 404

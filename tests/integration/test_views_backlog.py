"""观点积压：隐藏的不排，持仓和观察的先排、中文先排；几篇同时抽；只抽不写；论点只算名单里的公司。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from researchlib import theses
from researchlib import views as views_mod
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import enabled_sources, load_sources

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 编的英文长文，六千多字符，标题点名 Netflix。
ENGLISH = "\n\n".join(
    " ".join(f"Paragraph {index} sentence {line} explains the streaming argument in detail." for line in range(8))
    for index in range(16)
)


def _doc(external_id, title, body, day):
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
    store_documents(
        conn,
        source=sources[source_id],
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )


def _company(conn, name):
    return int(conn.execute("SELECT id FROM companies WHERE name_zh = %s", (name,)).fetchone()["id"])


def _ids(conn, external_ids):
    rows = conn.execute("SELECT id, external_id FROM articles WHERE external_id = ANY(%s)", (external_ids,)).fetchall()
    return {row["external_id"]: row["id"] for row in rows}


def test_pending_skips_hidden_and_puts_the_portfolio_and_chinese_first(conn, settings):
    _store(
        conn,
        settings,
        "fangwei-mp",
        [
            _doc("mp/meituan", "美团的配送", "美团的骑手补贴在变。美团的配送成本也在变。", 10),
            _doc("mp/tencent", "腾讯的广告", "腾讯的广告加载率还在提升。腾讯的视频号是新库存。", 1),
            _doc("mp/hidden", "美团再看", "美团的到店业务。美团的酒旅业务。", 11),
        ],
    )
    _store(conn, settings, "stratechery", [_doc("2025/netflix", "Netflix and the Bundle", ENGLISH, 12)])
    hide = Jsonb({"hidden": "short_post"})
    conn.execute("UPDATE articles SET raw_meta = raw_meta || %s WHERE external_id = 'mp/hidden'", (hide,))
    conn.commit()
    sources = [source.id for source in enabled_sources(load_sources(settings.sources_path))]
    companies = [_company(conn, name) for name in ("美团", "腾讯", "奈飞")]
    first = views_mod.portfolio_ids(conn, settings)
    assert _company(conn, "腾讯") in first and _company(conn, "美团") not in first
    rows = views_mod.pending_articles(conn, companies, sources, 10, first=first)
    ids = _ids(conn, ["mp/meituan", "mp/tencent", "mp/hidden", "2025/netflix"])
    # 腾讯在持仓里，虽然最早也排第一；中文的美团排在更新的英文 Netflix 前面；隐藏的那篇不排。
    assert [row["id"] for row in rows] == [ids["mp/tencent"], ids["mp/meituan"], ids["2025/netflix"]]
    assert views_mod.count_pending(conn, _company(conn, "美团"), sources) == 1
    # 不给名单时照旧只按中文、深入、新旧排。
    plain = views_mod.pending_articles(conn, companies, sources, 10)
    assert [row["id"] for row in plain] == [ids["mp/meituan"], ids["mp/tencent"], ids["2025/netflix"]]


def _reply(*_args, **_kwargs):
    view = {
        "about": True,
        "judgment": "腾讯的广告还有提价空间。",
        "mechanism": "视频号是新的广告库存。",
        "stance": "正面",
        "quote": "腾讯的广告加载率还在提升",
    }
    return json.dumps({"腾讯": view}, ensure_ascii=False), "fake-model"


def test_run_rows_in_parallel_and_dry_run_writes_nothing(conn, settings, monkeypatch, tmp_path):
    # 正文各不相同，免得四篇被认成同一篇。
    docs = [
        _doc(f"mp/tencent-{index}", f"腾讯第 {index} 篇", f"腾讯的广告加载率还在提升，第{index}季度更明显。", index + 1)
        for index in range(4)
    ]
    _store(conn, settings, "fangwei-mp", docs)
    monkeypatch.setattr(views_mod, "chat", _reply)
    sources = {source.id: source.author for source in enabled_sources(load_sources(settings.sources_path))}
    tencent = _company(conn, "腾讯")
    rows = views_mod.pending_articles(conn, [tencent], list(sources), 10)
    assert len(rows) == 4

    dry = views_mod.run_rows(settings, rows, sources, by_ids=False, workers=2, dry_run=True)
    assert dry["done"] == 4 and dry["failed"] == 0 and dry["unavailable"] is None
    assert len(dry["records"]) == 4
    record = dry["records"][0]
    assert record["views"]["腾讯"]["stance"] == "positive" and record["views"]["腾讯"]["quote_found"] is True
    assert conn.execute("SELECT count(*) AS n FROM company_views").fetchone()["n"] == 0

    done = views_mod.run_rows(settings, rows, sources, by_ids=False, workers=2)
    assert done["done"] == 4 and done["failed"] == 0
    conn.commit()
    written = conn.execute("SELECT count(*) AS n FROM company_views WHERE company_id = %s", (tencent,)).fetchone()
    assert written["n"] == 4
    assert views_mod.pending_articles(conn, [tencent], list(sources), 10) == []


def test_theses_portfolio_takes_only_listed_companies_with_enough_views(conn, settings):
    docs = [
        _doc(f"mp/both-{index}", f"腾讯和美团第 {index} 篇", f"腾讯的广告第 {index} 次。美团的配送第 {index} 次。", day)
        for index, day in enumerate(range(1, 6))
    ]
    _store(conn, settings, "fangwei-mp", docs)
    tencent, meituan = _company(conn, "腾讯"), _company(conn, "美团")
    for row in conn.execute("SELECT id, content_hash FROM articles WHERE external_id LIKE 'mp/both-%'").fetchall():
        for company_id in (tencent, meituan):
            conn.execute(
                """
                INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
                VALUES (%s, %s, %s, %s, '判断', 'positive')
                """,
                (row["id"], company_id, row["content_hash"], views_mod.PROMPT_VERSION),
            )
    conn.commit()
    source_ids = [source.id for source in enabled_sources(load_sources(settings.sources_path))]
    everyone = {int(row["id"]) for row in theses.companies_with_views(conn, source_ids)}
    assert {tencent, meituan} <= everyone
    chosen = theses.portfolio_companies(conn, settings, source_ids)
    assert [int(row["id"]) for row in chosen] == [tencent]
    # 隐藏的文章不算观点：腾讯的五篇都藏起来，就不够数了。
    conn.execute("UPDATE articles SET raw_meta = raw_meta || %s", (Jsonb({"hidden": "short_post"}),))
    conn.commit()
    assert theses.portfolio_companies(conn, settings, source_ids) == []

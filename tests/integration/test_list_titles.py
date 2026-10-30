"""卡片以外的列表（作者索引、重复文章页、接口）和卡片用同一套星球标题；重复文章页不列站内隐藏的读者帖。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

CUT = "Temu：值得重视的关税利好 ..."
WHOLE = "Temu：值得重视的关税利好"
BODY = (
    "Temu：值得重视的关税利好\n\n"
    "中美这次国事访问后达成的对等降税安排，主要是针对几类商品：小家电、玩具、节庆装饰品等。\n"
)


def _store(conn, settings, source_id, docs):
    source = next(item for item in load_sources(settings.sources_path) if item.id == source_id)
    store_documents(
        conn,
        source=source,
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )
    conn.commit()


def _row(conn, external_id):
    return conn.execute(
        "SELECT id, title, duplicate_of FROM articles WHERE external_id = %s", (external_id,)
    ).fetchone()


def test_lists_outside_cards_show_the_repaired_star_title(conn, settings):
    when = datetime(2026, 9, 27, 14, 41, tzinfo=SHANGHAI)
    _store(conn, settings, "chengfengpolang", [ParsedDoc("cf-temu", CUT, when, BODY, {"format": "markdown"}, [])])
    article_id = _row(conn, "cf-temu")["id"]
    with TestClient(create_app(settings)) as client:
        authors = client.get("/authors").text
        listing = client.get("/api/v1/articles", params={"author": "乘风破浪"}).json()
        hits = client.get("/api/v1/search", params={"q": "对等降税"}).json()
        passages = client.get("/api/v1/search", params={"q": "对等降税", "level": "chunk"}).json()
        single = client.get(f"/api/v1/articles/{article_id}").json()
        markdown = client.get(f"/api/v1/articles/{article_id}.md").text
    assert WHOLE in authors and CUT not in authors
    assert [item["title"] for item in listing["items"]] == [WHOLE]
    assert [item["title"] for item in hits["items"] if item["id"] == article_id] == [WHOLE]
    assert {p["title"] for p in passages["passages"] if p["article_id"] == article_id} == {WHOLE}
    assert single["title"] == WHOLE and f'title: "{WHOLE}"' in markdown
    # 库里的标题不改。
    assert _row(conn, "cf-temu")["title"] == CUT


def test_duplicates_page_skips_hidden_reader_posts(conn, settings):
    when = datetime(2025, 3, 12, 9, 0, tzinfo=SHANGHAI)
    reader = {"format": "markdown", "topic_id": "5550001", "poster_name": "王小明"}
    host = {"format": "markdown", "topic_id": "5550002", "poster_name": "方伟Rey"}
    asked = "王小明想问：拼多多的主站货币化率还能涨多少，三季度的广告收入会不会放缓？\n"
    answered = "拼多多的二季度：主站的货币化率还有空间……\n\n" + "广告库存还在增加，商家愿意出的价格也更高。\n"
    _store(
        conn,
        settings,
        "fangwei-zsxq",
        [
            ParsedDoc("2025/reader.md", "王小明的提问", when, asked, dict(reader), []),
            ParsedDoc("5550001", "王小明的提问", when, asked, dict(reader), []),
            ParsedDoc("2025/host.md", "拼多多的二季度：主站的货币化率还有空间……", when, answered, dict(host), []),
            ParsedDoc("5550002", "拼多多的二季度：主站的货币化率还有空间……", when, answered, dict(host), []),
        ],
    )
    reader_copy, reader_main = _row(conn, "2025/reader.md"), _row(conn, "5550001")
    host_copy, host_main = _row(conn, "2025/host.md"), _row(conn, "5550002")
    assert reader_copy["duplicate_of"] == reader_main["id"] and host_copy["duplicate_of"] == host_main["id"]
    with TestClient(create_app(settings)) as client:
        page = client.get("/duplicates").text
        only = client.get("/duplicates", params={"article": reader_main["id"]}).text
    assert f'id="a-{host_main["id"]}"' in page and f'href="/articles/{host_copy["id"]}"' in page
    assert "拼多多的二季度：主站的货币化率还有空间" in page and "空间……" not in page
    assert f'/articles/{reader_main["id"]}"' not in page and "王小明" not in page
    assert "王小明" not in only and "没有认出重复的文章" in only

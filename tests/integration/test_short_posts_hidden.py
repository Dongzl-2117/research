"""短于来源 min_chars 的帖子入库时标 hidden=short_post：不进首页、作者页和检索，正文页还开得开。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

LONG = (
    "白酒的渠道库存去年就开始堆起来了，批价往下走是迟早的事。"
    "经销商手里压着货，厂家还在往下压任务，等到动销跟不上，"
    "第一步是批价松动，第二步是打款变慢，第三步才轮到报表。"
    "所以看这门生意，先看批价和渠道库存，比看季度利润早半年。\n"
)


def _doc(external_id: str, title: str, body: str, when: datetime) -> ParsedDoc:
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=when,
        body_md=body,
        raw_meta={"format": "markdown", "filename": f"{external_id}.md"},
        image_refs=[],
    )


def test_short_post_is_hidden_but_still_opens(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "heiheiwuchang")
    assert source.min_chars == 100
    when = datetime(2026, 10, 28, 9, 30, tzinfo=SHANGHAI)
    store_documents(
        conn,
        source=source,
        docs=[
            _doc("short-1", "批价又松了", "批价又松了，渠道库存还在堆。\n", when),
            _doc("long-1", "先看批价和渠道库存", LONG, when.replace(hour=10)),
        ],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    rows = {
        row["external_id"]: row
        for row in conn.execute("SELECT id, external_id, raw_meta FROM articles").fetchall()
    }
    assert rows["short-1"]["raw_meta"]["hidden"] == "short_post"
    assert "hidden" not in rows["long-1"]["raw_meta"]
    short_id = rows["short-1"]["id"]
    long_id = rows["long-1"]["id"]

    app = create_app(settings)
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert f"/articles/{short_id}" not in home.text
        assert f"/articles/{long_id}" in home.text

        author = client.get("/authors/黑黑无常")
        assert author.status_code == 200
        assert f"/articles/{short_id}" not in author.text
        assert f"/articles/{long_id}" in author.text

        found = client.get("/search", params={"q": "批价"})
        assert found.status_code == 200
        assert f"/articles/{short_id}" not in found.text
        assert f"/articles/{long_id}" in found.text

        page = client.get(f"/articles/{short_id}")
        assert page.status_code == 200
        assert "短帖，暂只收长文。这篇不出现在按天、作者列表和检索里。" in page.text
        assert "渠道库存还在堆" in page.text
        normal = client.get(f"/articles/{long_id}")
        assert "这篇不出现在按天" not in normal.text

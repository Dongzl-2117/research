"""检索按片段直达（A）：?chunk=A&to=B 标上这几块，切对照／中文／原文时带着落点，序号这类小差异也标得中。"""

from dataclasses import asdict, replace
from datetime import UTC, datetime

import yaml
from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import Source, sync_sources
from researchlib.web.app import create_app


def setup_sources(conn, settings, tmp_path):
    sources = [Source("landing-a", "虚构札记甲", "王小明", "markdown", True)]
    path = tmp_path / "landing-sources.yaml"
    path.write_text(yaml.safe_dump({"sources": [asdict(s) for s in sources]}, allow_unicode=True))
    sync_sources(conn, sources)
    conn.commit()
    return replace(settings, sources_path=path), sources


def _store(conn, settings, source: Source, body: str) -> int:
    store_documents(conn, source=source, docs=[ParsedDoc(
        "landing", "落点札记", datetime(2026, 1, 1, tzinfo=UTC), body, {"format": "markdown"}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    return conn.execute("SELECT id FROM articles WHERE external_id='landing'").fetchone()["id"]


def test_chunk_range_and_ordered_lists_land_on_the_passage(conn, settings, tmp_path):
    settings, sources = setup_sources(conn, settings, tmp_path)
    paragraphs = [f"第 {index} 段：王小明讲渠道费用和会员费，" + "经营数据见年报。" * 14 for index in range(1, 13)]
    listed = "\n".join(f"{index}. 第 {index} 条：会员续费率高，和商品毛利是两回事，" + "细节见附注。" * 8
                       for index in range(1, 4))
    body = "\n\n".join(paragraphs[:6]) + "\n\n" + listed + "\n\n" + "\n\n".join(paragraphs[6:])
    aid = _store(conn, settings, sources[0], body)
    rows = conn.execute(
        "SELECT ord, body FROM article_chunks WHERE article_id=%s AND coalesce(lang,'')='' ORDER BY ord", (aid,),
    ).fetchall()
    assert len(rows) >= 4
    with_list = next(row for row in rows if "1. 第 1 条" in row["body"])
    with TestClient(create_app(settings)) as client:
        # 块里写着「1.」，页面上是 <ol>：以前一字不差才算，落在页面顶上。
        page = client.get(f"/articles/{aid}?chunk={with_list['ord']}").text
        assert 'class="is-quoted"' in page and 'class="quoted"' not in page
        # 一个片段几块：都标上，滚到第一块。
        first, last = rows[1]["ord"], rows[3]["ord"]
        single = client.get(f"/articles/{aid}?chunk={first}").text.count("is-quoted")
        span = client.get(f"/articles/{aid}?chunk={first}&to={last}").text.count("is-quoted")
        assert span > single
        # to 超出范围就只标第一块。
        assert client.get(f"/articles/{aid}?chunk={first}&to={first + 50}").text.count("is-quoted") == single
        # 读者没有对照时不出语言标签；落点参数照常可用，页面不报错。
        assert client.get(f"/articles/{aid}?chunk={first}&to=abc").status_code == 200

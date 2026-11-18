"""超长文节选页（检索按片段直达，B）：带着 ?chunk= 打开几十万字的资料只渲染那一节，留「看全文」。"""

from dataclasses import asdict, replace
from datetime import UTC, datetime

import yaml
from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import Source, sync_sources
from researchlib.web.app import create_app


def _setup(conn, settings, tmp_path):
    sources = [Source("excerpt-a", "虚构资料甲", "王小明", "markdown", True)]
    path = tmp_path / "excerpt-sources.yaml"
    path.write_text(yaml.safe_dump({"sources": [asdict(s) for s in sources]}, allow_unicode=True))
    sync_sources(conn, sources)
    conn.commit()
    return replace(settings, sources_path=path), sources[0]


def _store(conn, settings, source, key: str, body: str) -> int:
    store_documents(conn, source=source, docs=[ParsedDoc(
        key, "很长的资料", datetime(2026, 1, 1, tzinfo=UTC), body, {"format": "markdown"}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    return conn.execute("SELECT id FROM articles WHERE external_id=%s", (key,)).fetchone()["id"]


def _body(sections: int) -> str:
    parts = []
    for index in range(sections):
        parts.append(f"## 第{index}章 话题{index}")
        for line in range(3):
            lead = f"甲{index}乙{line}：王小明讲第{index}章的渠道费用，"
            parts.append(lead + f"第{index}章的经营数据见年报附注。" * 5)
    return "\n\n".join(parts)


def test_long_article_opens_on_the_section(conn, settings, tmp_path):
    settings, source = _setup(conn, settings, tmp_path)
    aid = _store(conn, settings, source, "long", _body(120))
    rows = conn.execute(
        "SELECT ord, heading FROM article_chunks WHERE article_id=%s AND ord < 10000 ORDER BY ord", (aid,),
    ).fetchall()
    assert len(rows) > 80
    target = next(row for row in rows if row["heading"] == "第70章 话题70")
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{aid}?chunk={target['ord']}").text
        assert "节选 · 第" in page and "看全文" in page
        assert "甲70乙0" in page and "甲10乙0" not in page and "甲100乙0" not in page
        assert 'class="is-quoted"' in page and 'class="quoted"' not in page
        assert f'href="/articles/{aid}?section=' in page
        full = client.get(f"/articles/{aid}?chunk={target['ord']}&full=1").text
        assert "节选 · 第" not in full and "甲10乙0" in full and "甲100乙0" in full
        assert 'class="is-quoted"' in full
        # 目录、上一节下一节按节号走。
        section = client.get(f"/articles/{aid}?section=5").text
        assert "甲5乙0" in section and "节选 · 第 6 /" in section and "甲70乙0" not in section
        # 不带参数直接打开仍是全文。
        assert "节选 · 第" not in client.get(f"/articles/{aid}").text
    short = _store(conn, settings, source, "short", _body(8))
    first = conn.execute("SELECT min(ord) AS o FROM article_chunks WHERE article_id=%s", (short,)).fetchone()["o"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{short}?chunk={first}").text
        assert "节选 · 第" not in page and 'class="is-quoted"' in page


def test_repeated_citation_opens_its_own_chapter(conn, settings, tmp_path):
    settings, source = _setup(conn, settings, tmp_path)
    repeated = "这一组公司的历史交易记录见年报附注，投资者应当核对收购时点以及出价。"
    body = "\n\n".join((
        _body(90), "## 甲公司研究", "### 收购记录", repeated,
        "## 乙公司研究", "### 收购记录", repeated,
        "## 丙公司研究", "### 收购记录", repeated,
    ))
    aid = _store(conn, settings, source, "repeated", body)
    rows = conn.execute(
        "SELECT ord FROM article_chunks WHERE article_id=%s AND heading='收购记录' ORDER BY ord",
        (aid,),
    ).fetchall()
    assert len(rows) == 3
    with TestClient(create_app(settings)) as client:
        for row, parent in zip(rows, ("甲公司研究", "乙公司研究", "丙公司研究"), strict=True):
            page = client.get(f"/articles/{aid}?chunk={row['ord']}").text
            breadcrumb = page.split('<p class="excerpt-path">', 1)[1].split("</p>", 1)[0]
            assert parent in breadcrumb
            assert 'class="is-quoted"' in page
            if parent == "乙公司研究":
                # 第二章的节选还附带第三章的同一段话，高亮仍在第二章。
                prose = page.split('<div class="prose"', 1)[1].split('</div>', 1)[0]
                assert prose.index("is-quoted") < prose.index("丙公司研究")

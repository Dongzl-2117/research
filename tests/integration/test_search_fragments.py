"""检索按片段列（C）：结果是一篇里专门讲这件事的那一段，链接带起止块号，可展开，一篇多处给「这篇还有 N 处」。"""

from dataclasses import asdict, replace
from datetime import UTC, datetime

import yaml
from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import Source, sync_sources
from researchlib.web.app import create_app


def _setup(conn, settings, tmp_path):
    sources = [Source("frag-a", "虚构资料甲", "王小明", "markdown", True)]
    path = tmp_path / "frag-sources.yaml"
    path.write_text(yaml.safe_dump({"sources": [asdict(s) for s in sources]}, allow_unicode=True))
    sync_sources(conn, sources)
    conn.commit()
    return replace(settings, sources_path=path), sources[0]


def _store(conn, settings, source, key, title, body, day=1):
    store_documents(conn, source=source, docs=[ParsedDoc(
        key, title, datetime(2026, 1, day, tzinfo=UTC), body, {"format": "markdown"}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    conn.commit()
    return conn.execute("SELECT id FROM articles WHERE external_id=%s", (key,)).fetchone()["id"]


def _section(name: str, topic: bool, count: int = 3) -> str:
    lines = [f"## {name}"]
    for index in range(count):
        if topic:
            # 每节用词不同，免得被当成同一段话的重复收掉。
            filler = "".join(f"{name}{index}之{n}要算清回报。" for n in range(20))
            lines.append(f"{name}第{index}段：资本配置看管理层把现金投到哪里，回购、分红还是并购。" + filler)
        else:
            lines.append(f"{name}第{index}段：这一节讲门店选址和客流，" + "和别的事情无关的内容。" * 30)
    return "\n\n".join(lines)


def test_fragments_link_to_the_passage_and_expand(conn, settings, tmp_path):
    settings, source = _setup(conn, settings, tmp_path)
    body = "\n\n".join([
        _section("门店", False), _section("资本配置的三种做法", True), _section("客流", False),
        _section("回购的时机", True, 1), _section("选址", False), _section("并购的教训", True, 1),
        _section("杂谈", False), _section("分红政策", True, 1), _section("尾声", False),
    ])
    long_id = _store(conn, settings, source, "long", "投资札记汇编", body)
    _store(conn, settings, source, "short", "短帖", "资本配置要看回报率，管理层别乱花钱。", day=2)
    with TestClient(create_app(settings)) as client:
        page = client.get("/search", params={"q": "资本配置"}).text
        assert "处片段，来自" in page and "按文章看" in page
        # 章节名当标题，链接带起止块号，连着几块拼成一段。
        assert "<mark>资本配置</mark>的三种做法</a></h3>" in page
        assert f"/articles/{long_id}?chunk=" in page and "&amp;to=" in page
        assert "连着 " in page and "<details" in page
        # 一篇最多三条，其余收进「这篇还有 N 处」，链到只在这篇里找。
        assert page.count(f'href="/articles/{long_id}?chunk=') <= 3
        assert "这篇还有 1 处" in page and f"article={long_id}" in page
        # 只在这一篇里找：不封顶，默认按原文顺序。
        inside = client.get("/search", params={"q": "资本配置", "article": long_id}).text
        assert "只在《" in inside and "按原文顺序" in inside
        assert inside.count(f'href="/articles/{long_id}?chunk=') == 4
        order = [inside.find(name) for name in ("的三种做法第", "回购的时机", "并购的教训", "分红政策")]
        assert order == sorted(order) and min(order) > 0
        # 按文章看：原来的一篇一张卡片。
        cards = client.get("/search", params={"q": "资本配置", "view": "article"}).text
        assert "篇全部命中" in cards and "按片段" in cards
        # 接口：level=passage。
        api = client.get("/api/v1/search", params={"q": "资本配置", "level": "passage"}).json()
        first = next(p for p in api["passages"] if p["article_id"] == long_id)
        assert first["from"] <= first["chunk"] <= first["to"] and "chunk=" in first["url"]
        assert api["articles"] == 2

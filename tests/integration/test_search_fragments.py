"""检索按片段列（C）：结果是一篇里专门讲这件事的那一段，链接带起止块号，可展开，一篇多处给「这篇还有 N 处」。"""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from html import unescape
from re import search
from urllib.parse import parse_qs, urlsplit

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
        empty = client.get("/search").text
        assert 'name="view" value="article"' not in empty
        assert 'name="view" value="article"' in client.get("/search?view=article").text
        page = client.get("/search", params={"q": "资本配置"}).text
        assert "处片段，来自" in page and "按文章看" in page
        # 章节名当标题，链接带起止块号，连着几块拼成一段。
        assert "<mark>资本配置</mark>的三种做法</a></h3>" in page
        assert f"/articles/{long_id}?chunk=" in page and "&amp;to=" in page
        assert "连着 " in page and "<details" in page
        # 点进文章再回来：检索页记下地址、滚动和展开的片段，文章页据此放「返回检索结果」。
        assert 'src="/static/search.js?v=' in page
        # 一篇最多三条，其余收进「这篇还有 N 处」，链到只在这篇里找。
        assert page.count(f'href="/articles/{long_id}?chunk=') <= 3
        assert "这篇还有 1 处" in page and f"article={long_id}" in page
        # 只在这一篇里找：不封顶，默认按原文顺序。
        inside = client.get("/search", params={"q": "资本配置", "article": long_id}).text
        assert "只在《" in inside and "按原文顺序" in inside
        assert inside.count(f'href="/articles/{long_id}?chunk=') == 4
        ask_link = unescape(search(r'<a class="ask-card" href="([^"]+)"', inside)[1])
        assert parse_qs(urlsplit(ask_link).query) == {"q": ["资本配置"], "article": [str(long_id)]}
        composer = client.get(ask_link).text
        assert f'name="article" value="{long_id}"' in composer
        assert "只用这篇：投资札记汇编" in composer
        assert composer.index("公开问库：") < composer.index('id="ask-q"')
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


def test_one_long_section_takes_one_card_and_a_paragraph_long_heading_is_not_the_title(conn, settings, tmp_path):
    settings, source = _setup(conn, settings, tmp_path)
    # 飞书笔记常把一整段话标成标题（库里最长 200 字）：不拿它当卡片标题。
    sentence = "资本配置的要点" + "，先看回购再看分红最后看并购的回报" * 6
    body = "\n\n".join([
        _section("资本配置的三种做法", True, 16), _section("门店", False), _section(sentence, True, 1),
    ])
    article = _store(conn, settings, source, "sections", "投资札记汇编", body)
    with TestClient(create_app(settings)) as client:
        inside = client.get("/search", params={"q": "资本配置", "article": article}).text
        pieces = inside.count("的三种做法</a></h3>")
        assert pieces >= 2  # 这一节比 6 块长，被切成了几截
        page = client.get("/search", params={"q": "资本配置"}).text
        # 同一节只列一张卡片，别的几截记进「这篇还有 N 处」。
        assert page.count("的三种做法</a></h3>") == 1
        assert f"这篇还有 {pieces - 1} 处" in page
        # 一整段话的章节名：标题用文章名，章节名放在下面一行。
        assert ">投资札记汇编</a></h3>" in page
        assert f'<p class="item-heading" title="{sentence}">' in page
        assert "最后看并购的回报</a></h3>" not in page


def test_search_drilldown_keeps_date_source_author_and_selected_sort(conn, settings, tmp_path):
    settings, source = _setup(conn, settings, tmp_path)
    body = "\n\n".join(_section(f"章节{i}", i % 2 == 0) for i in range(8))
    article = _store(conn, settings, source, "scoped", "多处证据", body)
    filters = {"q": "资本配置", "from": "2026-01-01", "to": "2026-01-02",
               "source": source.id, "author": source.author}

    def linked_params(html, pattern):
        url = unescape(search(pattern, html)[1])
        return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

    with TestClient(create_app(settings)) as client:
        start = client.get("/search", params=filters).text
        scoped = linked_params(start, r'class="fragment-rest"><a href="([^"]+)"')
        assert scoped == {**filters, "article": str(article)}
        inside = client.get("/search", params=scoped).text
        scored = linked_params(inside, r'<a href="([^"]+)">按相关度</a>')
        chosen = {**scoped, "sort": "score"}
        assert scored == chosen
        inside = client.get("/search", params=scored).text
        assert 'name="sort" value="score"' in inside
        assert linked_params(inside, r'<a class="ask-card" href="([^"]+)"') == scoped
        cards = linked_params(inside, r'<a href="([^"]+)">按文章看</a>')
        assert cards == {**chosen, "view": "article"}
        article_view = client.get("/search", params=cards).text
        assert linked_params(article_view, r'<a href="([^"]+)">按片段</a>') == chosen
        assert linked_params(inside, r'<a href="([^"]+)">清除</a>') == {
            "q": "资本配置", "source": source.id, "article": str(article), "sort": "score",
        }


def test_expired_search_page_returns_the_last_page_with_its_results(conn, settings, tmp_path, monkeypatch):
    from researchlib.web import routes

    settings, source = _setup(conn, settings, tmp_path)
    for i in (1, 2):
        _store(conn, settings, source, f"note-{i}", f"资本配置笔记{i}", f"资本配置和回购的不同方法{i}。", day=i)
    monkeypatch.setattr(routes, "SEARCH_PAGE_SIZE", 1)
    with TestClient(create_app(settings)) as client:
        for view in ("passage", "article"):
            params = {"q": "资本配置", "view": view, "source": source.id, "page": "9"}
            page = client.get("/search", params=params)
            assert [item.status_code for item in page.history] == [303]
            assert dict(page.request.url.params) == {**params, "page": "2"}
            assert 'class="here num">2<' in page.text and 'class="item-title"' in page.text
            assert "没有找到" not in page.text

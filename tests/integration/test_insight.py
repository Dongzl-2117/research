"""观点用起来：论点地图、首页持仓近况的最近判断、文章页同期别人怎么看、MCP 取观点；
长文的输入；失败记次数；出问题才提示；引用落到正文那一段。"""

import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib import digest as digest_mod
from researchlib import theses as theses_mod
from researchlib import views as views_mod
from researchlib.failures import clear_failure, note_failure
from researchlib.health import write_status
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
DOCS = [
    ("fangwei-mp", "mp/t1", 1, "腾讯的广告加载率还有提升空间，腾讯视频号的商业化刚开始，腾讯的游戏也稳住了。"),
    ("chengfengpolang", "cf/t2", 6, "腾讯的估值已经不便宜，腾讯的广告增长在放缓，腾讯回购的力度很大。"),
    ("gudujinhuati", "gd/t3", 12, "腾讯的视频号就是新的朋友圈，腾讯会把交易接进来，腾讯的广告库存还会涨。"),
    ("fangwei-zsxq", "zx/t4", 20, "腾讯的游戏版号恢复了，腾讯的新游戏排期很满，腾讯明年的收入会更好看。"),
]


def _store_all(conn, settings):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    for source_id, external_id, day, body in DOCS:
        source = replace(sources[source_id], owner_ids=("1001",))
        doc = ParsedDoc(
            external_id=external_id,
            title=f"腾讯笔记 {external_id}",
            published_at=datetime(2026, 9, day, 9, 0, tzinfo=SHANGHAI),
            body_md=body,
            raw_meta={"poster_id":"1001"},
            image_refs=[],
        )
        store_documents(conn, source=source, docs=[doc], data_dir=settings.data_dir,
                        archive_files=1, images_in_archive=0, errors=[])


def _tencent(conn):
    return conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '腾讯'").fetchone()


def _row(conn, external_id):
    return conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id FROM articles WHERE external_id = %s",
        (external_id,),
    ).fetchone()


JUDGMENTS = {
    "mp/t1": "mp/t1 的判断：视频号广告库存还没到顶，加载率还能抬",
    "cf/t2": "cf/t2 的判断：视频号广告库存见顶了，加载率抬不上去",
    "gd/t3": "gd/t3 的判断：视频号就是新的广告库存，交易进来以后出价会更高",
    "zx/t4": "zx/t4 的判断：游戏版号恢复了，新游戏的排期很满",
}


def _views(conn, settings, monkeypatch):
    stances = {"mp/t1": "正面", "cf/t2": "负面", "gd/t3": "正面", "zx/t4": "两面"}
    authors = {source.id: source.author for source in load_sources(settings.sources_path)}
    company = _tencent(conn)
    for _source, external_id, _day, body in DOCS:
        reply = json.dumps(
            {"腾讯": {"judgment": JUDGMENTS[external_id], "mechanism": "广告库存和定价",
                      "stance": stances[external_id], "quote": body[:20]}},
            ensure_ascii=False,
        )
        monkeypatch.setattr(views_mod, "chat", lambda _s, _m, reply=reply, **_k: (reply, "grok-test"))
        row = _row(conn, external_id)
        views_mod.extract_article(conn, settings, row, authors[row["source_id"]], [company])
    return company


def test_theses_views_home_and_same_period(conn, settings, monkeypatch):
    _store_all(conn, settings)
    company = _views(conn, settings, monkeypatch)
    ids = {external_id: _row(conn, external_id)["id"] for _s, external_id, _d, _b in DOCS}
    reply = json.dumps(
        {"theses": [
            {"title": "视频号广告库存", "mechanism": "加载率还能抬，出价会更高",
             "views": [f"v{ids['mp/t1']}", f"v{ids['gd/t3']}", f"v{ids['cf/t2']}", f"v{ids['zx/t4']}"]},
            {"title": "游戏版号恢复", "mechanism": "新游戏排期很满", "views": [f"v{ids['zx/t4']}"]},
        ]},
        ensure_ascii=False,
    )
    monkeypatch.setattr(theses_mod, "chat", lambda _s, _m, **_k: (reply, "grok-test"))
    sources_by_id = {source.id: source for source in enabled_sources(load_sources(settings.sources_path))}
    # 只有一篇的「游戏版号」论点丢掉；游戏那篇也不该被塞进广告库存。
    assert theses_mod.build(conn, settings, company, sources_by_id) == 1
    assert not theses_mod.needs_build(conn, company["id"], sources_by_id, settings)
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/companies/{company['id']}").text
        assert 'id="theses"' in page and "视频号广告库存" in page and "加载率还能抬" in page
        assert "游戏版号恢复" not in page.split('id="views"', 1)[0]
        assert page.index('id="theses"') < page.index('id="views"')
        only = client.get(f"/companies/{company['id']}", params={"views": "t1"}).text
        section = only.split('id="views"', 1)[1].split('class="company-articles"', 1)[0]
        assert "mp/t1 的判断" in section and "cf/t2 的判断" in section and "zx/t4 的判断" not in section
        # 全部时间线仍能看到没进论点的那篇。
        everything = page.split('id="views"', 1)[1].split('class="company-articles"', 1)[0]
        assert "zx/t4 的判断" in everything
        # 首页最近观点：按观点时间排，不写「持仓近况」。
        home = client.get("/").text
        assert "最近观点" in home and "持仓近况" not in home
        assert "holdings-row" in home and "近 30 天" in home and "zx/t4 的判断" not in home
        assert home.find("最近观点") < home.find('class="feed"')
        # 文章页：同期别的作者怎么看，不列自己。
        article = client.get(f"/articles/{ids['mp/t1']}").text
        assert "同期别人怎么看" in article and "cf/t2 的判断" in article and "mp/t1 的判断" not in article
        # JSON 和 MCP。
        data = client.get(f"/api/v1/companies/{company['id']}/views", params={"views": "t1"}).json()
        assert data["theses"][0]["title"] == "视频号广告库存" and data["total"] == 3
        assert data["theses"][0]["against"]["count"] == 1 and data["theses"][0]["for"]["count"] == 2
        assert client.get(f"/api/v1/companies/{company['id']}/views", params={"views": "x"}).status_code == 400
        call = client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "get_company_views", "arguments": {"id": company["id"]}}},
        ).json()
        assert call["result"]["structuredContent"]["theses"][0]["title"] == "视频号广告库存"


def test_blank_views_skip_theses_and_refresh_blank_requeues(conn, settings):
    """判断或态度为空白的观点不进论点；删掉之后才会重新排队。"""
    _store_all(conn, settings)
    tencent = _tencent(conn)
    articles = [_row(conn, external_id) for _source, external_id, _day, _body in DOCS]
    blanks = articles[:2]
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
        VALUES (%s, %s, %s, %s, '   ', ''), (%s, %s, %s, %s, '', '')
        """,
        (
            blanks[0]["id"], tencent["id"], blanks[0]["content_hash"], views_mod.PROMPT_VERSION,
            blanks[1]["id"], tencent["id"], blanks[1]["content_hash"], views_mod.PROMPT_VERSION,
        ),
    )
    conn.commit()
    sources = {source.id: source for source in enabled_sources(load_sources(settings.sources_path))}
    assert theses_mod.views_for(conn, tencent["id"], sources, settings) == []
    source_ids = list(sources)
    pending = [row["id"] for row in views_mod.pending_articles(conn, [tencent["id"]], source_ids, 20)]
    assert blanks[0]["id"] not in pending and blanks[1]["id"] not in pending
    assert views_mod.delete_blank_views(conn, [tencent["id"]]) == 2
    pending = [row["id"] for row in views_mod.pending_articles(conn, [tencent["id"]], source_ids, 20)]
    assert blanks[0]["id"] in pending and blanks[1]["id"] in pending


def test_long_articles_feed_the_ending_and_the_company_paragraphs(conn, settings, monkeypatch):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    sections = [f"## 第 {index} 节\n\n" + f"第 {index} 节讲的是行业格局的变化，" * 60 for index in range(1, 90)]
    body = "\n\n".join(sections) + "\n\n## 结论\n\n结论：腾讯的广告还有空间，这是全文最后的判断。"
    doc = ParsedDoc(external_id="mp/long", title="长文", published_at=datetime(2026, 9, 1, tzinfo=SHANGHAI),
                    body_md=body, raw_meta={}, image_refs=[])
    store_documents(conn, source=sources["fangwei-mp"], docs=[doc], data_dir=settings.data_dir,
                    archive_files=1, images_in_archive=0, errors=[])
    row = _row(conn, "mp/long")
    assert len(row["body_text"]) > digest_mod.BODY_LIMIT
    seen = []

    def capture(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary": "导读", "points": ["一"], "tags": ["腾讯"]}', "grok-test"

    monkeypatch.setattr(digest_mod, "chat", capture)
    digest_mod.digest_article(conn, settings, row, "方伟")
    assert "全文最后的判断" in seen[0] and "（长文" in seen[0] and len(seen[0]) < digest_mod.BODY_LIMIT + 2000
    saved = conn.execute("SELECT head_only FROM article_digests WHERE article_id = %s", (row["id"],)).fetchone()
    assert saved["head_only"] is False
    # 观点：只给提到这几家公司的段落加开头结尾。
    prompts = []
    monkeypatch.setattr(views_mod, "chat", lambda _s, messages, **_k: (prompts.append(messages[1]["content"]) or (
        '{"腾讯": {"judgment": "广告还有空间", "stance": "正面"}}', "grok-test")))
    views_mod.extract_article(conn, settings, row, "方伟", [_tencent(conn)])
    assert "全文最后的判断" in prompts[0] and len(prompts[0]) < views_mod.BODY_LIMIT + 2000
    # 以前只读了开头的，标成 head_only，重新排队。
    conn.execute("UPDATE article_digests SET head_only = true WHERE article_id = %s", (row["id"],))
    conn.commit()
    pending = [item["id"] for item in digest_mod.pending_articles(conn, ["fangwei-mp"], 10)]
    assert row["id"] in pending
    # 失败记次数：冷却期里不再排队，成功后清掉。
    note_failure(conn, "digest", row["id"], "Grok 接口返回 503")
    assert row["id"] not in [item["id"] for item in digest_mod.pending_articles(conn, ["fangwei-mp"], 10)]
    clear_failure(conn, "digest", row["id"])
    conn.commit()
    assert row["id"] in [item["id"] for item in digest_mod.pending_articles(conn, ["fangwei-mp"], 10)]


def test_home_only_speaks_up_when_something_is_wrong(conn, settings):
    _store_all(conn, settings)
    with TestClient(create_app(settings)) as client:
        assert 'class="notice is-alert"' not in client.get("/").text
        write_status(settings.data_dir, "backup", False, "导出数据库失败（退出码 1）：连不上数据库")
        home = client.get("/").text
        assert 'class="notice is-alert"' in home and "备份失败" in home and "连不上数据库" in home
        write_status(settings.data_dir, "backup", True, "7.5M，4 篇")
        assert 'class="notice is-alert"' not in client.get("/").text


def test_quoted_chunk_lands_on_its_paragraph(conn, settings):
    _store_all(conn, settings)
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    paragraphs = [f"第 {index} 段：" + "腾讯的广告和视频号。" * 30 for index in range(1, 9)]
    doc = ParsedDoc(external_id="mp/quote", title="引用", published_at=datetime(2026, 9, 2, tzinfo=SHANGHAI),
                    body_md="\n\n".join(paragraphs), raw_meta={}, image_refs=[])
    store_documents(conn, source=sources["fangwei-mp"], docs=[doc], data_dir=settings.data_dir,
                    archive_files=1, images_in_archive=0, errors=[])
    article_id = _row(conn, "mp/quote")["id"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}", params={"chunk": 1}).text
        assert "is-quoted" in page and 'class="quoted"' not in page and "scrollIntoView" in page
        plain = client.get(f"/articles/{article_id}").text
        assert "is-quoted" not in plain and "scrollIntoView" not in plain


def test_company_page_does_not_auto_schedule_theses_by_default(conn, settings, monkeypatch):
    """公司页不后台排队补论点，旧的自动开关也不能让 GET 花钱。"""
    assert settings.theses_auto_enabled is False
    _store_all(conn, settings)
    company = _tencent(conn)
    calls = []
    monkeypatch.setattr(theses_mod, "needs_build", lambda *a, **k: True)
    monkeypatch.setattr(theses_mod, "schedule", lambda *a, **k: calls.append(True) or True)
    monkeypatch.setattr(theses_mod, "stored", lambda *a, **k: None)
    monkeypatch.setattr(theses_mod, "present", lambda *a, **k: [])

    with TestClient(create_app(settings)) as client:
        assert client.get(f"/companies/{company['id']}").status_code == 200
    assert calls == []

    from dataclasses import replace

    enabled = replace(settings, theses_auto_enabled=True)
    with TestClient(create_app(enabled)) as client:
        assert client.get(f"/companies/{company['id']}").status_code == 200
    assert calls == []


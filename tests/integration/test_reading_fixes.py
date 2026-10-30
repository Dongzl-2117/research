"""相对 .md 链接、飞书目录树、知识星球真实钟点，以及不限持仓的最近观点。"""

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.mdlinks import relative_md_hrefs
from researchlib.migrate import MIGRATIONS, split_sql
from researchlib.retime import refresh_clocks
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.views import PROMPT_VERSION, pending_articles, timeline
from researchlib.web.app import create_app


def _source(settings, source_id):
    return next(source for source in load_sources(settings.sources_path) if source.id == source_id)


def _store(conn, settings, source_id, docs):
    store_documents(
        conn,
        source=_source(settings, source_id),
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )


def test_article_1297_page_has_no_relative_md_hrefs(conn, settings):
    feishu = "linwenfeng-feishu"
    docs = [
        ParsedDoc(
            external_id="docs/零售研究__JsoOabc1.md",
            title="零售研究",
            published_at=None,
            body_md="零售正文。\n",
            raw_meta={"format": "markdown", "parent": "docs", "filename": "零售研究__JsoOabc1.md"},
            image_refs=[],
        ),
        ParsedDoc(
            external_id="docs/新标题__Tokn1234.md",
            title="新标题",
            published_at=None,
            body_md="新标题正文。\n",
            raw_meta={"format": "markdown", "parent": "docs", "filename": "新标题__Tokn1234.md"},
            image_refs=[],
        ),
    ]
    for index in range(40):
        name = f"文档{index}__Tok{index:04d}.md"
        docs.append(
            ParsedDoc(
                external_id=f"docs/{name}",
                title=f"文档{index}",
                published_at=None,
                body_md="目标。\n",
                raw_meta={"format": "markdown", "parent": "docs", "filename": name},
                image_refs=[],
            )
        )
    lines = [
        "见 [零售研究](零售研究__JsoOabc1.md)。",
        "",
        "[旧标题](旧标题__Tokn1234.md)",
        "",
        "[外链](https://example.com/files/note.md)",
        "",
        "[段落](/articles/1?chunk=3#p-2)",
        "",
    ]
    lines.extend(f"- [没有的文档{i}](没有的文档{i}__Nope{i:04d}.md)" for i in range(80))
    lines.extend(f"- [文档{i}](文档{i}__Tok{i:04d}.md)" for i in range(40))
    docs.append(
        ParsedDoc(
            external_id="docs/主页-Jason 林文丰__root.md",
            title="主页",
            published_at=None,
            body_md="\n".join(lines) + "\n",
            raw_meta={"format": "markdown", "parent": "docs", "filename": "主页-Jason 林文丰__root.md"},
            image_refs=[],
        )
    )
    _store(conn, settings, feishu, docs)
    article_id = conn.execute(
        "SELECT id FROM articles WHERE external_id = %s", ("docs/主页-Jason 林文丰__root.md",)
    ).fetchone()["id"]
    retail_id = conn.execute(
        "SELECT id FROM articles WHERE external_id = %s", ("docs/零售研究__JsoOabc1.md",)
    ).fetchone()["id"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article_id}")
        assert page.status_code == 200
        html = page.text
    assert relative_md_hrefs(html) == []
    assert f'href="/articles/{retail_id}"' in html
    assert 'href="https://example.com/files/note.md"' in html
    assert "?chunk=3" in html
    assert html.count("未收录") == 80
    assert 'id="p-' in html


def test_feishu_tree_page_nests_first_referrer(conn, settings):
    from researchlib.doctree import build_doc_tree

    feishu = "linwenfeng-feishu"
    _store(
        conn,
        settings,
        feishu,
        [
            ParsedDoc(
                external_id="docs/主页-Jason 林文丰__root.md",
                title="主页-Jason 林文丰",
                published_at=None,
                body_md="[零售](零售研究__a.md)\n\n[宏观](宏观__b.md)\n",
                raw_meta={"parent": "docs", "filename": "主页-Jason 林文丰__root.md", "format": "markdown"},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="docs/零售研究__a.md",
                title="零售研究",
                published_at=None,
                body_md="零售这一节。\n\n[渠道](渠道__c.md)\n",
                raw_meta={"parent": "docs", "filename": "零售研究__a.md", "format": "markdown"},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="docs/宏观__b.md",
                title="宏观",
                published_at=None,
                body_md="[零售](零售研究__a.md)\n",
                raw_meta={"parent": "docs", "filename": "宏观__b.md", "format": "markdown"},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="docs/渠道__c.md",
                title="渠道",
                published_at=None,
                body_md="渠道正文。\n",
                raw_meta={"parent": "docs", "filename": "渠道__c.md", "format": "markdown"},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="docs/孤立__z.md",
                title="孤立笔记",
                published_at=None,
                body_md="没人链到。\n",
                raw_meta={"parent": "docs", "filename": "孤立__z.md", "format": "markdown"},
                image_refs=[],
            ),
        ],
    )
    stats = build_doc_tree(conn, feishu)
    assert stats["orphans"] == 1 and stats["placed"] == 4
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/sources/{feishu}/tree")
        assert page.status_code == 200
        # 文库树只留公司/行业研究：「零售研究」留下，「宏观」「孤立笔记」藏掉。
        assert "零售研究" in page.text and "渠道" in page.text
        assert "孤立笔记" not in page.text and "未挂上目录" not in page.text
        assert "宏观" not in page.text.split("doc-nav", 1)[1].split("</nav>", 1)[0]
        retail = page.text.split("零售研究", 1)[1]
        assert "渠道" in retail
        child = conn.execute("SELECT id FROM articles WHERE title = '渠道'").fetchone()["id"]
        opened = client.get(f"/sources/{feishu}/tree/{child}")
        assert opened.status_code == 200 and "渠道正文" in opened.text
        author = client.get("/authors/林文丰")
        assert f"/sources/{feishu}/tree" in author.text
        home = client.get("/")
        assert "孤立笔记" not in home.text and "零售研究" not in home.text


def test_retime_reorders_stance_and_same_period(conn, settings, tmp_path: Path):
    midnight = datetime(2024, 11, 28, 0, 0, tzinfo=SHANGHAI)
    _store(
        conn,
        settings,
        "fangwei-zsxq",
        [
            ParsedDoc(
                external_id="111",
                title="早入库的晚文",
                published_at=midnight,
                body_md="腾讯早上还看好。\n",
                raw_meta={"filename": "2024-11-28-111.md", "date_source": "filename", "time_known": False},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="222",
                title="后入库的早文",
                published_at=midnight,
                body_md="腾讯其实先看淡。\n",
                raw_meta={"filename": "2024-11-28-222.md", "date_source": "filename", "time_known": False},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="INDEX",
                title="星球目录不该进按天",
                published_at=midnight,
                body_md="目录\n",
                raw_meta={"filename": "INDEX.md", "date_source": "filename", "time_known": False},
                image_refs=[],
            ),
        ],
    )
    _store(
        conn,
        settings,
        "chengfengpolang",
        [
            ParsedDoc(
                external_id="peer",
                title="乘风十一月",
                published_at=datetime(2024, 11, 10, 0, 0, tzinfo=SHANGHAI),
                body_md="腾讯同期看法。\n",
                raw_meta={},
                image_refs=[],
            )
        ],
    )
    tencent = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
    articles = {
        row["external_id"]: row
        for row in conn.execute(
            "SELECT id, external_id, content_hash, source_id FROM articles WHERE external_id = ANY(%s)",
            (["111", "222", "peer"],),
        ).fetchall()
    }
    for external_id, stance in (("111", "positive"), ("222", "negative"), ("peer", "negative")):
        row = articles[external_id]
        conn.execute(
            """
            INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (row["id"], tencent, row["content_hash"], PROMPT_VERSION, f"{external_id} 的判断", stance),
        )
        conn.execute(
            """
            INSERT INTO article_mentions (article_id, company_id, depth, hit_count)
            VALUES (%s, %s, 'section', 2)
            ON CONFLICT (article_id, company_id) DO UPDATE
              SET depth = 'section', hit_count = EXCLUDED.hit_count
            """,
            (row["id"], tencent),
        )
    conn.commit()
    sources = {source.id: source for source in load_sources(settings.sources_path) if source.enabled}
    before = [item["article_id"] for item in timeline(conn, tencent, sources, settings) if item["changed"]]
    assert before == [articles["222"]["id"]]

    archive = tmp_path / "zsxq"
    (archive / "md").mkdir(parents=True)
    (archive / "raw" / "topics").mkdir(parents=True)
    (archive / "md" / "111.md").write_text("腾讯早上还看好。\n", encoding="utf-8")
    (archive / "md" / "222.md").write_text("腾讯其实先看淡。\n", encoding="utf-8")
    (archive / "md" / "INDEX.md").write_text("目录\n", encoding="utf-8")
    (archive / "raw" / "group.json").write_text('{"group_id": 1}', encoding="utf-8")
    (archive / "raw" / "state.json").write_text('{"ok": true}', encoding="utf-8")
    (archive / "raw" / "topics" / "111.json").write_text(
        '{"topic_id": "111", "create_time": "2024-12-20T10:00:22.487+0800"}',
        encoding="utf-8",
    )
    (archive / "raw" / "topics" / "222.json").write_text(
        '{"topic_id": "222", "create_time": "2024-11-28T09:00:00.000+0800"}',
        encoding="utf-8",
    )
    from researchlib.ingest.parse import parse_archive

    docs, errors, _files, _images = parse_archive(archive, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    stats = refresh_clocks(conn, _source(settings, "fangwei-zsxq"), docs, settings)
    assert stats["removed_index"] == 1
    assert stats["updated"] >= 2
    assert stats["views"] >= 2
    after = [item["article_id"] for item in timeline(conn, tencent, sources, settings) if item["changed"]]
    assert after == [articles["111"]["id"]]
    changed_ids = {item["article_id"] for item in stats["changes"] if item["company_id"] == tencent}
    assert articles["111"]["id"] in changed_ids
    stored = conn.execute(
        "SELECT published_at, raw_meta, content_hash FROM articles WHERE external_id = '111'"
    ).fetchone()
    local = stored["published_at"].astimezone(SHANGHAI)
    assert (local.month, local.day, local.hour, local.minute) == (12, 20, 10, 0)
    assert stored["raw_meta"]["create_time"] == "2024-12-20T10:00:22.487+0800"
    assert stored["raw_meta"]["time_known"] is True
    view_hash = conn.execute(
        "SELECT content_hash FROM company_views WHERE article_id = %s", (articles["111"]["id"],)
    ).fetchone()["content_hash"]
    assert view_hash == stored["content_hash"]
    source_ids = list(sources)
    assert articles["111"]["id"] not in {row["id"] for row in pending_articles(conn, [tencent], source_ids, 20)}
    from researchlib.views import same_period

    peers = same_period(conn, articles["111"]["id"], None, sources, settings)
    assert all(item["article_id"] != articles["peer"]["id"] for item in peers)
    assert conn.execute("SELECT id FROM articles WHERE external_id = 'INDEX'").fetchone() is None
    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        assert "星球目录不该进按天" not in home


def test_retime_matches_dated_external_id_to_topics_json(conn, settings, tmp_path: Path):
    """库里是 YYYY-MM-DD_<topic_id>，时间在 raw/topics/<id>.json。不能只认纯数字外部 id。"""
    midnight = datetime(2024, 11, 28, 0, 0, tzinfo=SHANGHAI)
    _store(
        conn,
        settings,
        "fangwei-zsxq",
        [
            ParsedDoc(
                external_id="2024-11-28_888888",
                title="带日期前缀",
                published_at=midnight,
                body_md="文件名只有日期。\n",
                raw_meta={"filename": "2024-11-28_888888.md", "date_source": "filename", "time_known": False},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="999999",
                title="纯数字",
                published_at=midnight,
                body_md="纯数字外部 id。\n",
                raw_meta={"filename": "999999.md", "date_source": "filename", "time_known": False},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="2024-11-29_777777",
                title="没有 JSON 钟点",
                published_at=midnight,
                body_md="不该被编出时间。\n",
                raw_meta={"filename": "2024-11-29_777777.md", "date_source": "filename", "time_known": False},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="INDEX",
                title="星球目录不该进按天",
                published_at=midnight,
                body_md="目录\n",
                raw_meta={"filename": "INDEX.md"},
                image_refs=[],
            ),
        ],
    )
    archive = tmp_path / "zsxq"
    (archive / "md").mkdir(parents=True)
    (archive / "raw" / "topics").mkdir(parents=True)
    (archive / "md" / "2024-11-28_888888.md").write_text("文件名只有日期。\n", encoding="utf-8")
    (archive / "md" / "999999.md").write_text("纯数字外部 id。\n", encoding="utf-8")
    (archive / "md" / "777777.md").write_text("没有钟点。\n", encoding="utf-8")
    (archive / "md" / "INDEX.md").write_text("目录\n", encoding="utf-8")
    (archive / "raw" / "group.json").write_text("{}", encoding="utf-8")
    (archive / "raw" / "state.json").write_text("{}", encoding="utf-8")
    (archive / "raw" / "topics" / "888888.json").write_text(
        '{"topic_id": 888888, "create_time": "2024-11-28T10:40:22.487+0800"}',
        encoding="utf-8",
    )
    (archive / "raw" / "topics" / "999999.json").write_text(
        '{"topic_id": 999999, "create_time": "2024-11-28T09:05:00.000+0800"}',
        encoding="utf-8",
    )
    from researchlib.ingest.parse import parse_archive

    docs, errors, _files, _images = parse_archive(archive, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    stats = refresh_clocks(conn, _source(settings, "fangwei-zsxq"), docs, settings)
    assert stats["updated"] == 2
    assert stats["removed_index"] == 1
    dated = conn.execute(
        "SELECT external_id, published_at, raw_meta FROM articles WHERE external_id = %s",
        ("2024-11-28_888888",),
    ).fetchone()
    assert dated["external_id"] == "2024-11-28_888888"
    local = dated["published_at"].astimezone(SHANGHAI)
    assert (local.hour, local.minute, local.second) == (10, 40, 22)
    assert dated["raw_meta"]["create_time"] == "2024-11-28T10:40:22.487+0800"
    assert dated["raw_meta"]["time_known"] is True
    bare = conn.execute("SELECT published_at FROM articles WHERE external_id = '999999'").fetchone()
    assert bare["published_at"].astimezone(SHANGHAI).hour == 9
    untouched = conn.execute(
        "SELECT published_at, raw_meta FROM articles WHERE external_id = '2024-11-29_777777'"
    ).fetchone()
    assert untouched["published_at"].astimezone(SHANGHAI).hour == 0
    assert "create_time" not in (untouched["raw_meta"] or {})
    assert conn.execute("SELECT id FROM articles WHERE external_id = 'INDEX'").fetchone() is None


def test_retime_without_archive_reads_stored_header(conn, settings):
    """乘风破浪没有 raw JSON，归档头写「(UTC+8)」，以前读不出来，卡片时间是「—」。不给归档也能从库里改。"""
    midnight = datetime(2026, 9, 27, 0, 0, tzinfo=SHANGHAI)
    header = "# Temu：值得重视的关税利好\n\n- 作者：王小明\n- 时间：2026-09-27 14:41 (UTC+8)\n\n"
    _store(
        conn,
        settings,
        "chengfengpolang",
        [
            ParsedDoc(
                external_id="2026-09-27_14588211154841122",
                title="Temu：值得重视的关税利好",
                published_at=midnight,
                body_md=header + "腾讯的关税影响有限。\n",
                raw_meta={
                    "filename": "2026-09-27_14588211154841122.md",
                    "date_source": "filename",
                    "time_known": False,
                },
                image_refs=[],
            ),
            ParsedDoc(
                external_id="拼多多随想",
                title="外部 id 里没有话题号",
                published_at=midnight,
                body_md="- 时间：2026-09-27 09:05 (UTC+8)\n\n腾讯第二篇。\n",
                raw_meta={"date_source": "filename", "time_known": False},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="14588211154841122",
                title="同一话题，库里已有秒级钟点",
                published_at=datetime(2026, 9, 27, 14, 41, 22, tzinfo=SHANGHAI),
                body_md="- 时间：2026-09-27 14:41 (UTC+8)\n\n同一话题的另一行。\n",
                raw_meta={"create_time": "2026-09-27T14:41:22.487+0800", "date_source": "meta", "time_known": True},
                image_refs=[],
            ),
            ParsedDoc(
                external_id="2026-09-27_222",
                title="正文没有钟点",
                published_at=midnight,
                body_md="- 时间：2026-09-27\n\n不该被编出时间。\n",
                raw_meta={"filename": "2026-09-27_222.md", "date_source": "filename", "time_known": False},
                image_refs=[],
            ),
        ],
    )
    first = conn.execute(
        "SELECT id, content_hash FROM articles WHERE external_id = '2026-09-27_14588211154841122'"
    ).fetchone()
    tencent = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
        VALUES (%s, %s, %s, %s, '关税影响有限', 'positive')
        """,
        (first["id"], tencent, first["content_hash"], PROMPT_VERSION),
    )
    conn.commit()
    from researchlib.retime import docs_from_library

    stats = refresh_clocks(
        conn,
        _source(settings, "chengfengpolang"),
        docs_from_library(conn, "chengfengpolang"),
        settings,
        same_row=True,
    )
    assert stats["updated"] == 2
    assert stats["skipped_no_clock"] == 1
    assert stats["views"] == 1
    fixed = conn.execute(
        "SELECT published_at, content_hash, raw_meta FROM articles WHERE id = %s", (first["id"],)
    ).fetchone()
    local = fixed["published_at"].astimezone(SHANGHAI)
    assert (local.day, local.hour, local.minute) == (27, 14, 41)
    assert fixed["raw_meta"]["time_known"] is True
    assert fixed["raw_meta"]["date_source"] == "body"
    view_hash = conn.execute(
        "SELECT content_hash FROM company_views WHERE article_id = %s", (first["id"],)
    ).fetchone()["content_hash"]
    assert view_hash == fixed["content_hash"]
    loose = conn.execute("SELECT published_at FROM articles WHERE external_id = '拼多多随想'").fetchone()
    assert loose["published_at"].astimezone(SHANGHAI).hour == 9
    bare = conn.execute(
        "SELECT published_at, raw_meta FROM articles WHERE external_id = '2026-09-27_222'"
    ).fetchone()
    assert bare["published_at"].astimezone(SHANGHAI).hour == 0
    assert bare["raw_meta"]["time_known"] is False
    # 同话题的另一行本来就有秒级钟点：只改外部 id 相同的那一行，不把它截成整分。
    sibling = conn.execute(
        "SELECT published_at FROM articles WHERE external_id = '14588211154841122'"
    ).fetchone()
    assert sibling["published_at"].astimezone(SHANGHAI).second == 22
    # 改过的已经有真实钟点，再跑一遍不再看它们。
    again = docs_from_library(conn, "chengfengpolang")
    assert [doc.external_id for doc in again] == ["2026-09-27_222"]


def test_home_recent_views_include_non_holdings(conn, settings):
    when_old = datetime(2024, 5, 1, 9, 0, tzinfo=SHANGHAI)
    when_new = datetime(2024, 6, 1, 9, 0, tzinfo=SHANGHAI)
    # 摘要要挂出来，正文得够写摘要（几个字的帖不写摘要）。
    meituan_body = "美团还在。商家广告越来越贵，佣金率还在往上走，外卖份额守住了。\n"
    _store(
        conn,
        settings,
        "fangwei-mp",
        [
            ParsedDoc("hold", "腾讯旧观点", when_old, "腾讯还在。\n", {}, []),
            ParsedDoc("other", "美团新观点", when_new, meituan_body, {}, []),
        ],
    )
    rows = {
        row["external_id"]: row
        for row in conn.execute(
            "SELECT id, external_id, content_hash FROM articles WHERE external_id = ANY(%s)",
            (["hold", "other"],),
        ).fetchall()
    }
    names = {"hold": "腾讯", "other": "美团"}
    for external_id, name in names.items():
        company_id = conn.execute("SELECT id FROM companies WHERE name_zh = %s", (name,)).fetchone()["id"]
        row = rows[external_id]
        conn.execute(
            """
            INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
            VALUES (%s, %s, %s, %s, %s, 'positive')
            """,
            (row["id"], company_id, row["content_hash"], PROMPT_VERSION, f"{name}的判断"),
        )
        conn.execute(
            """
            INSERT INTO article_mentions (article_id, company_id, depth, hit_count)
            VALUES (%s, %s, 'section', 2)
            ON CONFLICT (article_id, company_id) DO UPDATE
              SET depth = 'section', hit_count = EXCLUDED.hit_count
            """,
            (row["id"], company_id),
        )
    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, points, tags, prompt_version)
        VALUES (%s, %s, 'grok-test', %s, %s, '[]', 2)
        """,
        (rows["other"]["id"], rows["other"]["content_hash"], "导读：美团的商家广告还在变贵。", Jsonb([])),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
    assert "持仓近况" not in home
    assert "role-badge" not in home
    section = home.split('aria-label="最近观点"', 1)[1].split('class="feed"', 1)[0]
    assert section.find("美团") < section.find("腾讯")
    assert 'class="recent-views-line"' in section
    assert "导读：美团的商家广告还在变贵。" in section
    assert 'class="recent-item-summary"' in section
    meituan = conn.execute("SELECT id FROM companies WHERE name_zh = '美团'").fetchone()["id"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/companies/{meituan}").text
    assert 'id="views"' in page and "美团的判断" in page
    assert "role-badge" not in page
    assert 'class="recent-lead"' in page
    assert "导读：美团的商家广告还在变贵。" in page


def test_recent_views_skip_a_digest_about_another_company(conn, settings):
    """美团只是文中的例子：这篇的导读讲拼多多，不放在美团下面，首页留空，公司页用这条判断。"""
    when = datetime(2024, 6, 1, 9, 0, tzinfo=SHANGHAI)
    _store(conn, settings, "fangwei-mp", [ParsedDoc("example", "拼多多的佣金", when, "拼多多和美团。\n", {}, [])])
    row = conn.execute("SELECT id, content_hash FROM articles WHERE external_id = 'example'").fetchone()
    meituan = conn.execute("SELECT id FROM companies WHERE name_zh = '美团'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance)
        VALUES (%s, %s, %s, %s, '外卖的补贴还要打很久', 'neutral')
        """,
        (row["id"], meituan, row["content_hash"], PROMPT_VERSION),
    )
    conn.execute(
        """
        INSERT INTO article_mentions (article_id, company_id, depth, hit_count)
        VALUES (%s, %s, 'section', 2)
        ON CONFLICT (article_id, company_id) DO UPDATE SET depth = 'section', hit_count = EXCLUDED.hit_count
        """,
        (row["id"], meituan),
    )
    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, points, tags, prompt_version)
        VALUES (%s, %s, 'grok-test', '导读：拼多多的佣金率还在往下压。', '[]', '[]', 2)
        """,
        (row["id"], row["content_hash"]),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        home = client.get("/").text
        page = client.get(f"/companies/{meituan}").text
    section = home.split('aria-label="最近观点"', 1)[1].split('class="feed"', 1)[0]
    assert "美团" in section
    assert "拼多多的佣金率" not in section and 'class="recent-item-summary"' not in section
    lead = page.split('class="recent-lead"', 1)[1][:600]
    assert "外卖的补贴还要打很久" in lead and "拼多多的佣金率" not in lead


def test_feishu_callout_marker_stays_out_of_cards_and_pages(conn, settings):
    """飞书高亮块导出成「> [!NOTE]」：新导入的正文不带标记，已入库的由迁移 022 清掉，公司页导读不再以「NOTE]」开头。"""
    body = "> [!NOTE]\n> 石油企业的运行九成是政治，一成是石油，美团也一样。\n\n后面是正文。\n"
    older = body.replace("石油企业", "旧的石油企业")
    docs = [
        ParsedDoc("docs/石油__Tok00001.md", "石油", None, body, {"format": "markdown"}, []),
        ParsedDoc("docs/旧文__Tok00002.md", "旧文", None, older, {"format": "markdown"}, []),
    ]
    _store(conn, settings, "linwenfeng-feishu", docs)
    fresh = conn.execute("SELECT body_text FROM articles WHERE external_id = 'docs/石油__Tok00001.md'").fetchone()
    assert "[!" not in fresh["body_text"]
    # 迁移前入库的正文还带着标记。
    conn.execute(
        "UPDATE articles SET body_text = '[!NOTE]' || chr(10) || body_text WHERE external_id = 'docs/旧文__Tok00002.md'"
    )
    for statement in split_sql((MIGRATIONS / "022_alert_markers.sql").read_text(encoding="utf-8")):
        conn.execute(statement)
    old = conn.execute("SELECT id, body_text FROM articles WHERE external_id = 'docs/旧文__Tok00002.md'").fetchone()
    assert old["body_text"].startswith("旧的石油企业的运行九成是政治")
    meituan = conn.execute("SELECT id FROM companies WHERE name_zh = '美团'").fetchone()["id"]
    conn.execute(
        "INSERT INTO article_mentions (article_id, company_id, depth, hit_count) VALUES (%s, %s, 'passing', 1)"
        " ON CONFLICT (article_id, company_id) DO NOTHING",
        (old["id"], meituan),
    )
    conn.commit()
    with TestClient(create_app(settings)) as client:
        company = client.get(f"/companies/{meituan}?depth=all").text
        article = client.get(f"/articles/{old['id']}").text
    assert "旧的石油企业的运行九成是政治" in company
    assert "NOTE]" not in company and "[!NOTE]" not in article
    assert "<blockquote" in article and "旧的石油企业的运行九成是政治" in article

"""归档自己的 README、INDEX 不是文章：导入不收，库里已有的导入时和上线时（迁移 035）删掉，飞书文档树不动。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import forget_archive_notes, store_documents
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
TENCENT = "腾讯的护城河是微信的关系链。腾讯游戏也在赚钱。"
README = "# 某公众号完整存档\n\n| 项目 | 值 |\n|---|---|\n| 已抓正文 | 2 |\n"
INDEX = f"# 存档索引\n\n| 日期 | 标题 |\n| --- | --- |\n| 2026-04-01 | {TENCENT} |\n"
TOPICS = f"# 话题索引（按时间倒序）\n\n- {TENCENT}\n"


def _doc(external_id, title, body, filename=None, published=True):
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2026, 4, 1, 9, 0, tzinfo=SHANGHAI) if published else None,
        body_md=body,
        raw_meta={"filename": filename or external_id.rsplit("/", 1)[-1]},
        image_refs=[],
    )


def _store(conn, settings, source_id, docs):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    return store_documents(
        conn,
        source=sources[source_id],
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
    )


def _ids(conn, source_id):
    rows = conn.execute("SELECT external_id FROM articles WHERE source_id = %s", (source_id,)).fetchall()
    return sorted(row["external_id"] for row in rows)


def _seed(conn, settings):
    """以前的导入把说明文件当成了文章：公众号根上的 README、INDEX（还有一份文件名一样、外部 id 被改名的），
    星球 md/ 里的 INDEX。飞书文档树里一层目录自己的 index.md 是正文。"""
    _store(
        conn,
        settings,
        "fangwei-mp",
        [
            _doc("README.md", "某公众号完整存档", README, published=False),
            _doc("INDEX.md", "存档索引", INDEX, published=False),
            _doc("INDEX.md-2", "存档索引", "# 存档索引\n\n腾讯、拼多多。\n", filename="INDEX.md", published=False),
            _doc("md/2026/2026-04-01-a.md", "腾讯的关系链", TENCENT),
        ],
    )
    _store(
        conn,
        settings,
        "chengfengpolang",
        [
            _doc("INDEX", "话题索引（按时间倒序）", TOPICS, filename="INDEX.md", published=False),
            _doc("888888", "拼多多的商家", "拼多多的商家在湖北。\n", filename="2026-04-01_888888.md"),
        ],
    )
    _store(
        conn,
        settings,
        "linwenfeng-feishu",
        [
            _doc("笔记/index.md", "总览", f"# 总览\n\n{TENCENT}\n", published=False),
            _doc("笔记/子文档/a.md", "子篇", "子文档。\n", published=False),
        ],
    )


def test_import_forgets_archive_notes_but_not_document_tree_pages(conn, settings):
    _seed(conn, settings)
    # 上面三次写入时说明文件已经进了库（导入前的那一步只删已有的）。
    assert "README.md" in _ids(conn, "fangwei-mp")
    note = conn.execute("SELECT id FROM articles WHERE external_id = 'INDEX.md'").fetchone()["id"]
    assert conn.execute("SELECT count(*) AS n FROM article_mentions WHERE article_id = %s", (note,)).fetchone()["n"]
    assert conn.execute("SELECT count(*) AS n FROM article_chunks WHERE article_id = %s", (note,)).fetchone()["n"]

    # 下一次导入公众号（抓取脚本每次都整目录重导）：说明文件从库里删掉，块和提及跟着删，文章不动。
    report = _store(conn, settings, "fangwei-mp", [_doc("md/2026/2026-04-02-b.md", "第二篇", "第二篇正文。\n")])
    assert report.removed_notes == 3
    assert "删掉说明文件: 3" in report.render()
    assert _ids(conn, "fangwei-mp") == ["md/2026/2026-04-01-a.md", "md/2026/2026-04-02-b.md"]
    for table in ("article_mentions", "article_chunks"):
        count = f"SELECT count(*) AS n FROM {table} WHERE article_id = %s"
        assert conn.execute(count, (note,)).fetchone()["n"] == 0, table

    # 星球的 INDEX（外部 id 是去掉扩展名的文件名）在这个来源下一次导入时删；别的来源不碰。
    assert _ids(conn, "chengfengpolang") == ["888888", "INDEX"]
    assert forget_archive_notes(conn, "chengfengpolang") == 1
    conn.commit()
    assert _ids(conn, "chengfengpolang") == ["888888"]

    # 飞书是文档树：导入不删 index.md。
    page = _doc("笔记/子文档/b.md", "又一篇", "又一篇。\n", published=False)
    report = _store(conn, settings, "linwenfeng-feishu", [page])
    assert report.removed_notes == 0
    assert _ids(conn, "linwenfeng-feishu") == ["笔记/index.md", "笔记/子文档/a.md", "笔记/子文档/b.md"]


def test_migration_035_removes_existing_notes_once_for_all_post_sources(conn, settings):
    from researchlib.migrate import MIGRATIONS, split_sql

    _seed(conn, settings)
    notes = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM articles WHERE external_id IN ('README.md', 'INDEX.md', 'INDEX.md-2', 'INDEX')"
        ).fetchall()
    ]
    assert len(notes) == 4
    tencent = conn.execute("SELECT id FROM companies WHERE name_zh = '腾讯'").fetchone()["id"]
    with TestClient(create_app(settings)) as client:
        # 修以前：说明文件能直接打开，挂在公司下面，检索搜得到。
        assert client.get(f"/articles/{notes[0]}").status_code == 200
        listed = {item["id"] for item in client.get(f"/api/v1/companies/{tencent}").json()["items"]}
        assert listed & set(notes)
        assert "存档索引" in client.get("/search", params={"q": "腾讯"}).text

    script = (MIGRATIONS / "035_forget_archive_notes.sql").read_text(encoding="utf-8")
    for _ in range(2):  # 能重复跑
        for statement in split_sql(script):
            conn.execute(statement)
        conn.commit()

    assert _ids(conn, "fangwei-mp") == ["md/2026/2026-04-01-a.md"]
    assert _ids(conn, "chengfengpolang") == ["888888"]
    assert _ids(conn, "linwenfeng-feishu") == ["笔记/index.md", "笔记/子文档/a.md"]
    for table in ("article_mentions", "article_chunks", "article_fingerprints"):
        left = conn.execute(f"SELECT count(*) AS n FROM {table} WHERE article_id = ANY(%s)", (notes,)).fetchone()["n"]
        assert left == 0, table
    with TestClient(create_app(settings)) as client:
        for note in notes:
            assert client.get(f"/articles/{note}").status_code == 404
            assert client.get(f"/api/v1/articles/{note}.md").status_code == 404
        listed = {item["id"] for item in client.get(f"/api/v1/companies/{tencent}").json()["items"]}
        assert listed and not listed & set(notes)
        assert "存档索引" not in client.get("/search", params={"q": "腾讯"}).text


def test_ingest_commands_skip_notes_for_post_archives_only(conn, settings, tmp_path, monkeypatch):
    from researchlib.ingest.feishu import main as feishu_main
    from researchlib.ingest.wechat import main as wechat_main

    for key, value in {
        "DATABASE_URL": settings.database_url,
        "DATA_DIR": str(settings.data_dir),
        "SOURCES_FILE": str(settings.sources_path),
    }.items():
        monkeypatch.setenv(key, value)
    # 公众号归档的样子：根上是抓取脚本写的说明和目录，文章在 md/ 下。
    inbox = tmp_path / "fangwei-mp"
    (inbox / "md" / "2026").mkdir(parents=True)
    (inbox / "README.md").write_text("# 某公众号完整存档\n\n| 已抓正文 | 1 |\n", encoding="utf-8")
    (inbox / "INDEX.md").write_text(f"# 存档索引\n\n- {TENCENT}\n", encoding="utf-8")
    (inbox / "md" / "2026" / "2026-04-01-a.md").write_text(
        f"# 腾讯的关系链\n\n- 作者：方伟看十年\n- 时间：2026-04-01 07:00:00 (UTC+8)\n\n{TENCENT}\n", encoding="utf-8"
    )
    assert wechat_main(["--source", "fangwei-mp", "--input", str(inbox)]) == 0
    conn.rollback()
    assert _ids(conn, "fangwei-mp") == ["md/2026/2026-04-01-a.md"]

    # 飞书文档树：一层目录自己的 index.md 照收。
    tree = tmp_path / "feishu"
    (tree / "笔记" / "子文档").mkdir(parents=True)
    (tree / "笔记" / "index.md").write_text(f"# 总览\n\n{TENCENT}\n", encoding="utf-8")
    (tree / "笔记" / "子文档" / "a.md").write_text("# 子篇\n\n子文档。\n", encoding="utf-8")
    assert feishu_main(["--source", "linwenfeng-feishu", "--input", str(tree)]) == 0
    conn.rollback()
    assert _ids(conn, "linwenfeng-feishu") == ["笔记/index.md", "笔记/子文档/a.md"]

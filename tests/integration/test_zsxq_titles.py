"""方伟星球卡片和文章页不显示被省略号截断的标题。库里的原标题先不动；夜间补好的标题重导时不改回去。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app
from researchlib.zsxq_repair import repair_zsxq
from researchlib.zsxq_title import stored_zsxq_title

FULL = "大模型生意的护城河在哪里，明确还不是一个能够回答的话题"
TRUNCATED = "大模型生意的护城河在哪里，明确…"
BODY = FULL + "。\n\n作者没有写出为什么现在答不了。\n"
DIGEST = "方伟认为，" + FULL + "。正文只给出这一判断，没有说明依据。"


def _source(settings):
    return next(source for source in load_sources(settings.sources_path) if source.id == "fangwei-zsxq")


def test_fangwei_cards_and_article_replace_cut_off_titles(conn, settings):
    when = datetime(2024, 10, 19, 10, 58, tzinfo=SHANGHAI)
    guest = ParsedDoc(
        "reader-cut",
        TRUNCATED,
        when,
        TRUNCATED + "\n",
        {"format": "markdown", "filename": "reader.md", "poster_name": "王小明"},
        [],
    )
    owner = ParsedDoc(
        "owner-cut",
        TRUNCATED,
        datetime(2024, 10, 19, 9, 0, tzinfo=SHANGHAI),
        BODY,
        {"format": "markdown", "filename": "owner.md"},
        [],
    )
    store_documents(
        conn,
        source=_source(settings),
        docs=[guest, owner],
        data_dir=settings.data_dir,
        archive_files=2,
        images_in_archive=0,
        errors=[],
    )
    guest_row = conn.execute(
        "SELECT id, title, content_hash FROM articles WHERE external_id = 'reader-cut'"
    ).fetchone()
    assert guest_row["title"] == TRUNCATED
    conn.execute(
        """
        INSERT INTO article_digests (
          article_id, content_hash, model, summary, points, tags, prompt_version, head_only, ocr_hash
        )
        VALUES (%s, %s, 'test', %s, '[]'::jsonb, '[]'::jsonb, 2, false, '')
        """,
        (guest_row["id"], guest_row["content_hash"], DIGEST),
    )
    conn.commit()
    owner_row = conn.execute("SELECT id, title FROM articles WHERE external_id = 'owner-cut'").fetchone()
    assert owner_row["title"] == TRUNCATED
    with TestClient(create_app(settings)) as client:
        home = client.get("/")
        assert home.status_code == 200
        # 读者帖已从网站隐藏：首页只剩星主卡片；截断标题仍由摘要补全。
        assert FULL in home.text
        assert "明确…" not in home.text
        assert "读者帖" not in home.text
        assert "王小明" not in home.text
        assert "research-recent-views" in home.text
        guest_page = client.get(f"/articles/{guest_row['id']}")
        assert guest_page.status_code == 404
        owner_page = client.get(f"/articles/{owner_row['id']}")
        assert owner_page.status_code == 200
        assert f"<h1>{FULL}</h1>" in owner_page.text
        assert "明确…" not in owner_page.text
    stored = conn.execute("SELECT title FROM articles WHERE external_id = 'reader-cut'").fetchone()
    assert stored["title"] == TRUNCATED


def test_retitled_article_drops_the_old_export_heading(conn, settings):
    """1273：导出标题「2026年9月1日」是正文开头的日期。页面换成正文那句后，正文顶上不再留一个日期大标题。"""
    chengfeng = next(source for source in load_sources(settings.sources_path) if source.id == "chengfengpolang")
    sentence = "瑞幸咖啡宣布其董事会已批准将股票回购计划的规模从$3亿扩大至$5亿"
    body = (
        "# 2026年9月1日\n\n"
        "- 作者：王小明\n"
        "- 时间：2026-09-01 18:57 (UTC+8)\n\n"
        f"2026年9月1日，{sentence}，并在2027年4月前新增授权金额2亿美元。该计划于2026年4月启动，为期12个月。\n"
    )
    store_documents(
        conn,
        source=chengfeng,
        docs=[
            ParsedDoc(
                "2026-09-01_55521141815842544",
                "2026年9月1日",
                datetime(2026, 9, 1, 18, 57, tzinfo=SHANGHAI),
                body,
                {"format": "markdown", "filename": "2026-09-01_55521141815842544.md"},
                [],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, title FROM articles WHERE external_id = '2026-09-01_55521141815842544'"
    ).fetchone()
    assert row["title"] == "2026年9月1日"
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}")
        assert page.status_code == 200
        assert f"<h1>{sentence}</h1>" in page.text
        assert ">2026年9月1日</h" not in page.text
        assert f"2026年9月1日，{sentence}" in page.text


def test_reimport_keeps_a_title_the_nightly_repaired(conn, settings):
    """星球抓取每次把整个存档重导一遍。只有图的帖导入时算不出标题，夜间回写按图片里的字补好以后，
    重导不能再把它改回「「图片」」，否则回写每次都要为同一批帖子重新起题（含 flash）。"""
    source = _source(settings)
    shot_only = "# 「图片」\n\n![](../../images/a.jpg)\n"

    def doc(body=shot_only):
        # 和导入一样：标题按 stored_zsxq_title 算，算不出就留导出的占位。
        when = datetime(2026, 10, 19, 18, 28, tzinfo=SHANGHAI)
        title = stored_zsxq_title("「图片」", body) or "「图片」"
        return ParsedDoc("12345678901234567", title, when, body, {"format": "markdown"}, [])

    def store(docs):
        return store_documents(
            conn,
            source=source,
            docs=docs,
            data_dir=settings.data_dir,
            archive_files=len(docs),
            images_in_archive=0,
            errors=[],
        )

    store([doc()])
    row = conn.execute("SELECT id, title FROM articles WHERE external_id = '12345678901234567'").fetchone()
    assert row["title"] == "「图片」"
    conn.execute(
        "UPDATE articles SET ocr_text = %s WHERE id = %s",
        ("拼多多的护城河在供应链，不在流量。", row["id"]),
    )
    stats = repair_zsxq(conn, "fangwei-zsxq", titles_only=True)
    assert stats["titles"] == 1
    repaired = conn.execute("SELECT title, content_hash FROM articles WHERE id = %s", (row["id"],)).fetchone()
    assert repaired["title"] == "拼多多的护城河在供应链，不在流量"
    conn.commit()

    report = store([doc()])
    assert report.unchanged == 1 and report.updated == 0
    again = conn.execute("SELECT title, content_hash FROM articles WHERE id = %s", (row["id"],)).fetchone()
    assert again == repaired
    assert repair_zsxq(conn, "fangwei-zsxq", titles_only=True)["titles"] == 0

    # 作者改了帖子（换了一张图）：库里那个补法对的是旧图，按导入的来，由夜间回写重新补。
    assert store([doc(shot_only.replace("a.jpg", "b.jpg"))]).updated == 1
    assert conn.execute("SELECT title FROM articles WHERE id = %s", (row["id"],)).fetchone()["title"] == "「图片」"
    # 导入自己就取得到一句的，照常用导入的标题。
    assert store([doc(shot_only + "\n补充一句：拼多多的库存还在去化。\n")]).updated == 1
    title = conn.execute("SELECT title FROM articles WHERE id = %s", (row["id"],)).fetchone()["title"]
    assert title == "补充一句：拼多多的库存还在去化"

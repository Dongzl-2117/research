"""补抽缺口：标题级提及、去重后的主版本要进队列；网友帖不进。OCR 旁路写进文章。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ocr import refresh_article_ocr
from researchlib.sources import load_sources
from researchlib.views import PROMPT_VERSION, gap_articles

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _store(conn, settings, source_id, external_id, title, body, raw_meta=None):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    doc = ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2026, 4, 1, 9, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta=raw_meta or {},
        image_refs=[],
    )
    store_documents(
        conn,
        source=sources[source_id],
        docs=[doc],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    return conn.execute(
        "SELECT id, content_hash FROM articles WHERE external_id = %s AND source_id = %s",
        (external_id, source_id),
    ).fetchone()


def test_gap_includes_title_mention_and_kept_original_but_not_guest(conn, settings):
    title_row = _store(conn, settings, "fangwei-mp", "gap-title", "贵州茅台还能看吗", "今天只写标题，正文先空着。\n")
    guest = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "gap-guest",
        "美亚光电和茅台",
        "读者转发：美亚光电的设备不错，顺带提到贵州茅台。\n",
        {"poster_name": "某读者", "poster_id": "8241"},
    )
    # 去重后留下的主版本：正文只有一句判断，提及可能是一带而过。
    kept = _store(
        conn,
        settings,
        "gudujinhuati",
        "gap-kept",
        "随手记",
        "今天市场很热闹。\n\n我还没有那么了解茅台。\n\n先观察到这里。\n",
    )
    duplicate = _store(
        conn,
        settings,
        "gudujinhuati",
        "gap-dup",
        "随手记",
        "今天市场很热闹。\n\n我还没有那么了解茅台。\n\n先观察到这里。\n",
    )
    conn.execute("UPDATE articles SET duplicate_of = %s WHERE id = %s", (kept["id"], duplicate["id"]))
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance, about)
        VALUES (%s, %s, %s, %s, '', '', true)
        """,
        (title_row["id"], company, title_row["content_hash"], PROMPT_VERSION),
    )
    conn.commit()
    guest_meta = conn.execute("SELECT raw_meta FROM articles WHERE id = %s", (guest["id"],)).fetchone()["raw_meta"]
    assert guest_meta["poster_role"] == "guest"
    rows = gap_articles(conn, [company], ["fangwei-mp", "fangwei-zsxq", "gudujinhuati"], 20)
    found = {row["id"] for row in rows}
    assert title_row["id"] in found
    assert kept["id"] in found
    assert guest["id"] not in found
    assert duplicate["id"] not in found


def test_comment_shell_views_are_neither_queued_nor_shown(conn, settings):
    """「图片」评论壳网站上不展示（文章页 404）：观点不补抽，库里已有的也不进公司页时间线和论点。"""
    from researchlib.theses import companies_with_views, views_for
    from researchlib.views import pending_articles, timeline

    meta = {"poster_name": "方伟Rey", "poster_id": "1001"}
    owner = _store(
        conn, settings, "fangwei-zsxq", "owner-moutai", "茅台批价",
        "贵州茅台的批价这一周没动，渠道仍在观望；茅台的提价空间要看飞天的动销。\n", meta,
    )
    shell = _store(
        conn, settings, "fangwei-zsxq", "shell-moutai", "「图片」 评论（3） - 李四",
        "贵州茅台的系列酒拖累了收入，茅台今年的增长主要靠直销渠道和i茅台。\n", meta,
    )
    assert shell["id"] != owner["id"]
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    sources = ["fangwei-zsxq"]
    assert {row["id"] for row in gap_articles(conn, [company], sources, 20)} == {owner["id"]}
    assert {row["id"] for row in pending_articles(conn, [company], sources, 20)} == {owner["id"]}

    for row in (owner, shell):
        conn.execute(
            """
            INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance, about)
            VALUES (%s, %s, %s, %s, '茅台的提价空间要看飞天的动销', 'positive', true)
            """,
            (row["id"], company, row["content_hash"], PROMPT_VERSION),
        )
    conn.commit()
    by_id = {source.id: source for source in load_sources(settings.sources_path) if source.id in sources}
    assert [item["article_id"] for item in timeline(conn, company, by_id, settings)] == [owner["id"]]
    assert [item["article_id"] for item in views_for(conn, company, by_id, settings)] == [owner["id"]]
    assert companies_with_views(conn, sources, minimum=2) == []


def test_star_nickname_posts_count_as_the_owners(conn, settings):
    """方伟在星球的显示名是「方伟Rey」。10 月 20 日以后按话题 JSON 导入的帖都被标成了读者帖，再导入一遍就改回来。"""
    body = "美亚光电的设备不错，贵州茅台的批价这一周没动，渠道仍在观望。\n"
    meta = {"poster_name": "方伟Rey", "poster_id": "1001"}
    row = _store(conn, settings, "fangwei-zsxq", "rey-post", "茅台批价", body, meta)
    role = "SELECT raw_meta->>'poster_role' AS role FROM articles WHERE id = %s"
    assert conn.execute(role, (row["id"],)).fetchone()["role"] == "owner"
    # 修好以前入库的：库里标着读者帖。正文没变，再导入一遍就改回原创，回到观点的补抽队列。
    conn.execute(
        "UPDATE articles SET raw_meta = raw_meta || '{\"poster_role\": \"guest\"}'::jsonb WHERE id = %s", (row["id"],)
    )
    conn.commit()
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    assert row["id"] not in {item["id"] for item in gap_articles(conn, [company], ["fangwei-zsxq"], 20)}
    _store(conn, settings, "fangwei-zsxq", "rey-post", "茅台批价", body, meta)
    assert conn.execute(role, (row["id"],)).fetchone()["role"] == "owner"
    assert row["id"] in {item["id"] for item in gap_articles(conn, [company], ["fangwei-zsxq"], 20)}


def test_ocr_side_field_is_what_views_read(conn, settings):
    row = _store(conn, settings, "fangwei-zsxq", "ocr-post", "图片", "（见图）\n")
    conn.execute(
        """
        INSERT INTO image_texts (content_hash, engine, text) VALUES ('abc', 'test', %s)
        """,
        ("茅台批价这一周没动，渠道仍在观望，短期不宜提价。",),
    )
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        VALUES (%s, 'fangwei-zsxq', 'pic.png', 'pic.png', 'abc')
        """,
        (row["id"],),
    )
    digest = refresh_article_ocr(conn, row["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text, ocr_hash FROM articles WHERE id = %s", (row["id"],)).fetchone()
    assert "不宜提价" in saved["ocr_text"] and saved["ocr_hash"] == digest
    # 空白观点在 OCR 哈希对不上时会重新进入缺口。
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO article_mentions (article_id, company_id, depth, hit_count)
        VALUES (%s, %s, 'title', 1)
        ON CONFLICT (article_id, company_id) DO UPDATE SET depth = 'title'
        """,
        (row["id"], company),
    )
    conn.execute(
        """
        INSERT INTO company_views (article_id, company_id, content_hash, prompt_version, judgment, stance, ocr_hash)
        VALUES (%s, %s, %s, %s, '', '', '')
        """,
        (row["id"], company, row["content_hash"], PROMPT_VERSION),
    )
    conn.commit()
    found = {item["id"] for item in gap_articles(conn, [company], ["fangwei-zsxq"], 10)}
    assert row["id"] in found


def test_new_star_nickname_relabels_stored_posts_at_startup(conn, settings, tmp_path):
    """sources.yaml 里加了星主的昵称，应用启动时按新名单把库里的帖改回原创，不用重新导入。"""
    from dataclasses import replace

    from researchlib.posters import refresh_poster_roles

    body = "美亚光电的设备不错，贵州茅台的批价这一周没动，渠道仍在观望。\n"
    rey = _store(conn, settings, "fangwei-zsxq", "rey-old", "茅台批价", body, {"poster_name": "方伟Rey"})
    question = "请问方老师，腾讯的视频号广告今年还能涨多少？\n"
    guest = _store(conn, settings, "fangwei-zsxq", "reader", "读者提问", question, {"poster_name": "王小明"})
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-zsxq")
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    # 修好以前的样子：名单里没有昵称，两篇都是读者帖。
    before = replace(source, poster_names=())
    refresh_poster_roles(conn, [before])
    conn.execute(
        "UPDATE articles SET raw_meta = raw_meta || '{\"poster_role\": \"guest\"}'::jsonb WHERE id = %s", (rey["id"],)
    )
    conn.commit()
    assert refresh_poster_roles(conn, [before]) == {}
    role = "SELECT raw_meta->>'poster_role' AS role FROM articles WHERE id = %s"
    assert conn.execute(role, (rey["id"],)).fetchone()["role"] == "guest"
    # 名单改了：只改对得上的那篇，读者帖不动；改回原创的回到观点补抽队列。
    assert refresh_poster_roles(conn, [source]) == {"fangwei-zsxq": 1}
    conn.commit()
    assert conn.execute(role, (rey["id"],)).fetchone()["role"] == "owner"
    assert conn.execute(role, (guest["id"],)).fetchone()["role"] == "guest"
    assert rey["id"] in {item["id"] for item in gap_articles(conn, [company], ["fangwei-zsxq"], 20)}
    assert refresh_poster_roles(conn, [source]) == {}


def test_demo_article_migration_only_removes_the_demo(conn, settings):
    from researchlib.migrate import MIGRATIONS, split_sql

    demo_body = "Aggregation Theory explains why platforms win.\n"
    demo = _store(conn, settings, "chengfengpolang", "demo.md", "Aggregation Theory (demo)", demo_body)
    kept = _store(conn, settings, "chengfengpolang", "real.md", "Aggregation Theory", "聚合理论讲平台为什么赢。\n")
    for statement in split_sql((MIGRATIONS / "023_drop_demo_article.sql").read_text(encoding="utf-8")):
        conn.execute(statement)
    conn.commit()
    ids = {row["id"] for row in conn.execute("SELECT id FROM articles WHERE source_id = 'chengfengpolang'").fetchall()}
    assert demo["id"] not in ids and kept["id"] in ids

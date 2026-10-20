"""保住全文：改稿存旧版，删稿、截短不覆盖；外链图片下载到本地；图片里的字进检索。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.parse import comments_section
from researchlib.ingest.store import store_documents
from researchlib.ocr import run_ocr
from researchlib.remote_images import FetchError, fetch_pending
from researchlib.retrieval import reset_stats_cache, search_articles
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope

SHANGHAI = ZoneInfo("Asia/Shanghai")
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
)
LONG = "\n\n".join(
    f"第 {index} 段：腾讯的广告加载率还在提升，视频号是新的广告库存，"
    f"商家的投放在往这里挪，第 {index} 个季度的数字更好。"
    for index in range(1, 12)
)


def _doc(external_id, body, *, title="腾讯的广告库存", refs=None, meta=None):
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2025, 3, 12, 9, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta=meta or {},
        image_refs=refs or [],
    )


def _store(conn, settings, source_id, docs, origin="archive"):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    return store_documents(
        conn,
        source=sources[source_id],
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=len(docs),
        images_in_archive=0,
        errors=[],
        origin=origin,
    )


def _article(conn, external_id):
    return conn.execute(
        "SELECT id, body_md, content_hash FROM articles WHERE external_id = %s", (external_id,)
    ).fetchone()


def _image(settings, name="pic.png"):
    path = settings.data_dir / "archive" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    return path


def test_reimport_keeps_the_full_text(conn, settings):
    _store(conn, settings, "fangwei-mp", [_doc("mp/a.md", LONG)])
    original = _article(conn, "mp/a.md")
    # 作者删了原文，导出工具抓到的是删除提示：不替换，只记一版。
    report = _store(conn, settings, "fangwei-mp", [_doc("mp/a.md", "该内容已被发布者删除")])
    assert report.held == 1 and report.updated == 0
    assert _article(conn, "mp/a.md")["body_md"] == LONG
    # 同一版再导一次也只记一次。
    assert _store(conn, settings, "fangwei-mp", [_doc("mp/a.md", "该内容已被发布者删除")]).held == 1
    # 截短到不到一半：也不替换。
    assert _store(conn, settings, "fangwei-mp", [_doc("mp/a.md", LONG[:200])]).held == 1
    # 正常改稿：替换，旧版存下来。
    edited = LONG.replace("第 3 段：腾讯", "第 3 段：改过以后，腾讯")
    report = _store(conn, settings, "fangwei-mp", [_doc("mp/a.md", edited)])
    assert report.updated == 1 and _article(conn, "mp/a.md")["body_md"] == edited
    rows = conn.execute(
        "SELECT kind, reason FROM article_revisions WHERE article_id = %s ORDER BY id", (original["id"],)
    ).fetchall()
    assert [(row["kind"], row["reason"]) for row in rows] == [
        ("held", "deleted"),
        ("held", "shrunk"),
        ("previous", ""),
    ]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{original['id']}").text
        assert 'class="revised"' in page and "后来被" in page
        diff = client.get(f"/articles/{original['id']}/revisions").text
        assert "改过以后" in diff and "该内容已被发布者删除" in diff and 'class="diff-del"' in diff
        assert client.get("/articles/999999/revisions").status_code == 404


def test_new_comment_replies_are_not_an_edit(conn, settings):
    replies = [("问一", "答一：主站的货币化率还有空间。")]
    first = LONG + comments_section(replies, LONG)
    _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:9", first)])
    article = _article(conn, "zsxq:9")
    # 评论里多了作者的回复：照常更新，不算改稿，不记旧版。
    more = LONG + comments_section([*replies, ("", "补充：关税的影响要分两段看。")], LONG)
    assert _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:9", more)]).updated == 1
    assert _article(conn, "zsxq:9")["body_md"] == more
    revisions = "SELECT kind FROM article_revisions WHERE article_id = %s ORDER BY id"
    assert conn.execute(revisions, (article["id"],)).fetchall() == []
    # 这次导出没带评论：库里那份更全，不动，也不算截短。
    report = _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:9", LONG)])
    assert report.unchanged == 1 and report.held == 0
    assert _article(conn, "zsxq:9")["body_md"] == more
    # 作者真的改了正文：照常替换并存旧版。
    edited = LONG.replace("第 3 段：腾讯", "第 3 段：改过以后，腾讯")
    assert _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:9", edited + comments_section(replies, edited))]).updated
    assert [row["kind"] for row in conn.execute(revisions, (article["id"],)).fetchall()] == ["previous"]
    # 正文被删成删稿提示、评论还在：照样拦下。
    gone = "该内容已被发布者删除" + comments_section(replies, "")
    assert _store(conn, settings, "fangwei-zsxq", [_doc("zsxq:9", gone)]).held == 1


def test_held_version_does_not_drop_the_images(conn, settings):
    image = _image(settings)
    _store(conn, settings, "fangwei-mp", [_doc("mp/b.md", LONG + "\n\n![](pic.png)", refs=[("pic.png", image)])])
    article = _article(conn, "mp/b.md")
    count = "SELECT count(*) AS n FROM article_images WHERE article_id = %s"
    assert conn.execute(count, (article["id"],)).fetchone()["n"] == 1
    _store(conn, settings, "fangwei-mp", [_doc("mp/b.md", "此内容因违规无法查看")])
    assert conn.execute(count, (article["id"],)).fetchone()["n"] == 1


def test_external_images_are_downloaded_and_shown(conn, settings):
    url = "https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png"
    body = f'<p>腾讯的广告收入。</p><p><img data-src="{url}" src="data:image/svg+xml,x"></p><p>视频号的库存。</p>'
    html = {"format": "html"}
    _store(conn, settings, "gudujinhuati", [_doc("aihot:1", body, meta=html)], origin="aihot")
    article = _article(conn, "aihot:1")
    calls = []

    def fake_fetch(address):
        calls.append(address)
        return PNG, "image/png"

    report = fetch_pending(conn, settings.data_dir, 50, fetch=fake_fetch)
    assert report["downloaded"] == 1 and calls == [url]
    row = conn.execute(
        "SELECT original_ref, storage_path, remote FROM article_images WHERE article_id = %s", (article["id"],)
    ).fetchone()
    assert row["remote"] and row["original_ref"] == url and row["storage_path"].startswith("remote/")
    assert (settings.data_dir / "images" / row["storage_path"]).is_file()
    # 再跑一遍：挂上的不再下载。
    assert fetch_pending(conn, settings.data_dir, 50, fetch=fake_fetch)["downloaded"] == 0 and len(calls) == 1
    # 正文改了重新导入：下载来的图不被归档图片的同步删掉。
    changed = body.replace("视频号的库存。", "视频号的库存还在涨。")
    _store(conn, settings, "gudujinhuati", [_doc("aihot:1", changed, meta=html)], origin="aihot")
    remote = "SELECT count(*) AS n FROM article_images WHERE article_id = %s AND remote"
    assert conn.execute(remote, (article["id"],)).fetchone()["n"] == 1
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article['id']}").text
        assert f'src="/media/{row["storage_path"]}"' in page and "mmbiz.qpic.cn" not in page
        assert client.get(f"/media/{row['storage_path']}").status_code == 200
    # 别的文章引用同一张图：直接复用，不再下载。
    _store(conn, settings, "gudujinhuati", [_doc("aihot:2", f'<p>转一张图</p><img src="{url}">', meta=html)],
           origin="aihot")
    assert fetch_pending(conn, settings.data_dir, 50, fetch=fake_fetch)["reused"] == 1 and len(calls) == 1
    # 下载失败：记下来，马上再跑不重试。
    _store(conn, settings, "gudujinhuati",
           [_doc("aihot:3", '<p>坏图</p><img src="https://example.com/gone.png">', meta=html)], origin="aihot")

    def failing(_address):
        raise FetchError("HTTP 404")

    assert fetch_pending(conn, settings.data_dir, 50, fetch=failing)["failed"] == 1
    again = fetch_pending(conn, settings.data_dir, 50, fetch=failing)
    assert again["failed"] == 0 and again["skipped"] == 1


def test_text_in_images_is_searchable(conn, settings):
    image = _image(settings, "chart.png")
    doc = _doc("mp/c.md", "腾讯的截图。\n\n![](chart.png)", refs=[("chart.png", image)])
    _store(conn, settings, "fangwei-mp", [doc])
    article = _article(conn, "mp/c.md")
    seen = "苹果服务收入占比提升到四分之一\n毛利率七成以上的软件生意"
    report = run_ocr(conn, settings.data_dir, 10, lambda _path: seen)
    assert report["images"] == 1 and report["with_text"] == 1
    chunk = conn.execute(
        "SELECT ord, anchor, heading FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article["id"],)
    ).fetchone()
    assert chunk["ord"] == 20000 and chunk["anchor"].startswith("img-") and chunk["heading"] == "图片里的字"
    # 同一张图不再识别。
    assert run_ocr(conn, settings.data_dir, 10, lambda _path: "不该再调用")["images"] == 0
    reset_stats_cache()
    sources = enabled_sources(load_sources(settings.sources_path))
    found = search_articles(conn, "毛利率七成", Scope(source_ids=[source.id for source in sources]), sources)
    assert article["id"] in [hit.article_id for hit in found.hits]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article['id']}", params={"chunk": chunk["ord"]}).text
        assert "图片里的字" in page and f'id="{chunk["anchor"]}"' in page

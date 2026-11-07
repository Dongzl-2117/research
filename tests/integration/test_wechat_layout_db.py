"""方伟公众号老文章按原文页摆图：写回库里、哈希跟过来、整目录重新导入时不退回估的位置。不连网。"""

from fastapi.testclient import TestClient

from researchlib import wechat_layout
from researchlib.ingest.parse import parse_archive
from researchlib.ingest.store import store_documents
from researchlib.render import image_anchor
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SN = "b" * 32
PARAS = [
    "茅台今年的批价一直在两千块上下，渠道库存不算高。",
    "我们先看一下过去五年的收入结构，直销占比提升很快。",
    "下面这张图是i茅台上线以后每个季度的直销收入。",
    "可以看到直销收入的增速明显快于整体收入增速。",
]
URL = f"https://mp.weixin.qq.com/s?__biz=MzI1&mid=2&idx=1&sn={SN}"


def _img(number: int) -> str:
    return f"https://mmbiz.qpic.cn/mmbiz_png/ID{number}xyz/640?wx_fmt=png"


# 极致了估的位置：两张图都堆在第一段后面。图是归档里带的本地文件，按先后对原文页上的图。
ESTIMATED = (
    f"# 茅台直销\n\n- 作者：方伟\n- 时间：2024-05-01 08:00:00 (UTC+8)\n- sn：`{SN}`\n"
    f"- 原链接：{URL.replace('&', '&amp;')}#rd\n\n"
    f"{PARAS[0]}\n\n![](images/1.png)\n\n![](images/2.png)\n\n{PARAS[1]}\n\n{PARAS[2]}\n\n{PARAS[3]}\n"
)
PAGE = (
    '<html><body><div class="rich_media_content" id="js_content">'
    f"<p>{PARAS[0]}</p><p>{PARAS[1]}</p><p>{PARAS[2]}</p>"
    f'<p><img data-src="{_img(1)}&amp;from=appmsg"></p><p>{PARAS[3]}</p>'
    f'<p><img data-src="{_img(2)}&amp;from=appmsg"></p>'
    "</div></body></html>"
)
VERIFY = "<html><body><p>环境异常</p><p>完成验证后即可继续访问</p></body></html>"


def _source(settings):
    return next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-mp")


def _import(conn, settings, tmp_path, body=ESTIMATED):
    archive = tmp_path / "inbox"
    folder = archive / "md" / "2024"
    (folder / "images").mkdir(parents=True, exist_ok=True)
    for number in (1, 2):
        (folder / "images" / f"{number}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([number]) * 24)
    (folder / f"2024-05-01-{SN}.md").write_text(body, encoding="utf-8")
    docs, errors, files, images = parse_archive(archive, tmp_path / "work", id_mode="path")
    assert errors == []
    return store_documents(
        conn,
        source=_source(settings),
        docs=docs,
        data_dir=settings.data_dir,
        archive_files=files,
        images_in_archive=images,
        errors=[],
        origin="crawl",
    )


def _row(conn):
    return conn.execute(
        "SELECT id, body_md, content_hash, raw_meta FROM articles WHERE source_id = 'fangwei-mp'"
    ).fetchone()


class Pages:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if not self.pages:
            raise AssertionError("不该再取原文页")
        return self.pages.pop(0)


def _relayout(conn, settings, fetch, **kwargs):
    return wechat_layout.relayout(
        conn, _source(settings), data_dir=settings.data_dir, fetch=fetch, pause=0, **kwargs
    )


def test_old_article_takes_image_positions_from_the_page_and_keeps_them(conn, settings, tmp_path):
    assert _import(conn, settings, tmp_path).inserted == 1
    before = _row(conn)
    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, points, tags, prompt_version)
        VALUES (%s, %s, 'test', '导读：直销占比提升推高毛利率。', '[]', '[]', 2)
        """,
        (before["id"], before["content_hash"]),
    )
    conn.commit()

    fetch = Pages(PAGE)
    report = _relayout(conn, settings, fetch)
    assert fetch.urls == [URL]
    assert report.waiting == 1 and len(report.placed) == 1 and report.images_moved == 2
    after = _row(conn)
    lines = [line for line in after["body_md"].split("\n") if line.strip()]
    assert lines.index("![](images/1.png)") == lines.index(PARAS[2]) + 1
    assert lines[-1] == "![](images/2.png)"
    assert after["raw_meta"]["images"] == "page" and after["content_hash"] != before["content_hash"]
    # 只是图挪了位置：不记旧版，摘要跟着新的哈希。
    assert conn.execute("SELECT count(*) AS n FROM article_revisions").fetchone()["n"] == 0
    digest = conn.execute("SELECT content_hash FROM article_digests WHERE article_id = %s", (after["id"],))
    assert digest.fetchone()["content_hash"] == after["content_hash"]

    # 抓取每次把整个归档目录重新导入：归档里还是估的位置，库里按原文页摆好的留着。
    again = _import(conn, settings, tmp_path)
    assert again.unchanged == 1 and again.updated == 0
    assert _row(conn)["body_md"] == after["body_md"]

    # 摆好的不再取原文页。
    assert _relayout(conn, settings, Pages()).waiting == 0

    hashes = {
        row["original_ref"]: row["content_hash"]
        for row in conn.execute(
            "SELECT original_ref, content_hash FROM article_images WHERE article_id = %s", (after["id"],)
        )
    }
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{after['id']}").text
    first, second = (f'<img id="{image_anchor(hashes[f"images/{n}.png"])}"' for n in (1, 2))
    marks = [PARAS[2], first, PARAS[3], second]
    assert [page.index(mark) for mark in marks] == sorted(page.index(mark) for mark in marks)


def test_author_edits_still_replace_a_placed_article(conn, settings, tmp_path):
    _import(conn, settings, tmp_path)
    _relayout(conn, settings, Pages(PAGE))
    edited = ESTIMATED.replace(PARAS[3], PARAS[3] + "后来补了一句：毛利率还会再涨。")
    assert _import(conn, settings, tmp_path, edited).updated == 1
    assert "后来补了一句" in _row(conn)["body_md"]


def test_dry_run_fetches_but_writes_nothing(conn, settings, tmp_path):
    _import(conn, settings, tmp_path)
    before = _row(conn)
    report = _relayout(conn, settings, Pages(PAGE), dry_run=True)
    assert len(report.placed) == 1 and "--dry-run" in report.render()
    assert _row(conn) == before


def test_verification_page_stops_the_run_and_marks_nothing(conn, settings, tmp_path):
    _import(conn, settings, tmp_path)
    report = _relayout(conn, settings, Pages(VERIFY))
    assert report.stopped.startswith("微信要求验证") and report.looked == 0
    assert "images_checked" not in _row(conn)["raw_meta"]
    assert _relayout(conn, settings, Pages(PAGE)).placed


def test_unplaceable_article_is_remembered_and_not_fetched_again(conn, settings, tmp_path):
    _import(conn, settings, tmp_path)
    # 原文页上是另一篇（原链接指错了）：字对不上，不挪。
    other = PAGE
    for para, replacement in zip(PARAS, (
        "这是另一篇文章，讲的是腾讯的广告业务，今年视频号涨得很快。",
        "视频号的广告加载率还很低，朋友圈的广告价格今年也没涨。",
        "所以广告收入的增长主要靠视频号，明年还能再涨三成。",
        "游戏和金融科技两块今年都比较平，云业务利润率在往上走。",
    ), strict=True):
        other = other.replace(para, replacement)
    report = _relayout(conn, settings, Pages(other))
    assert len(report.unplaced) == 1 and "对得上原文页" in report.unplaced[0]
    meta = _row(conn)["raw_meta"]
    assert meta["images_checked"]["version"] == wechat_layout.LAYOUT_VERSION and "images" not in meta
    assert _relayout(conn, settings, Pages()).waiting == 0

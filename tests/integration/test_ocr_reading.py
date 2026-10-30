"""阅读页把截图里的字展开；摘要和观点的输入同样读到这些字。

样例按 14886 的形状：正文一句，判断在群聊截图里。公司提及表仍不看图片。
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from researchlib import digest as digest_mod
from researchlib import views as views_mod
from researchlib.chunks import replace_ocr_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ocr import refresh_article_ocr
from researchlib.render import image_anchor
from researchlib.sources import load_sources
from researchlib.web.app import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")
CHAT = (
    "微信\n20:41\n张三\n瑞幸和古茗都算头部，但瑞幸同店还有压力\n"
    "李四\n古茗店效还行，瑞幸的折扣不能再深了\n张三\n先看到这里，不展开了\n"
)
JUDGMENT = "张三\n茅台批价这一周没动，渠道仍在观望\n李四\n短期不宜提价\n王五\n先看到这里\n"


def _png(path: Path, marker: bytes) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker)


def _store(conn, settings, source_id: str, external_id: str, title: str, body: str, images: list[Path]):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    doc = ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=datetime(2026, 10, 18, 9, 0, tzinfo=SHANGHAI),
        body_md=body,
        raw_meta={},
        image_refs=[(path.name, path) for path in images],
    )
    store_documents(
        conn,
        source=sources[source_id],
        docs=[doc],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=len(images),
        errors=[],
    )
    return conn.execute(
        "SELECT id, content_hash, source_id FROM articles WHERE external_id = %s AND source_id = %s",
        (external_id, source_id),
    ).fetchone()


def _remember(conn, article_id: int, ref: str, text: str) -> str:
    row = conn.execute(
        "SELECT content_hash FROM article_images WHERE article_id = %s AND original_ref = %s",
        (article_id, ref),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO image_texts (content_hash, engine, text) VALUES (%s, 'test', %s)
        ON CONFLICT (content_hash) DO UPDATE SET text = EXCLUDED.text
        """,
        (row["content_hash"], text),
    )
    return row["content_hash"]


def test_reader_shows_chat_text_and_collapses_the_screenshot(conn, settings, tmp_path):
    chat = tmp_path / "chat.png"
    logo = tmp_path / "logo.png"
    _png(chat, b"\x00" * 24)
    _png(logo, b"\x01" * 24)
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-14886",
        "图片",
        "瑞幸古茗是比较头部的。\n\n![](chat.png)\n\n![](logo.png)\n",
        [chat, logo],
    )
    chat_hash = _remember(conn, row["id"], "chat.png", CHAT)
    logo_hash = _remember(conn, row["id"], "logo.png", "logo")
    refresh_article_ocr(conn, row["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (row["id"],)).fetchone()
    assert "瑞幸和古茗都算头部" in saved["ocr_text"]
    assert "logo" not in saved["ocr_text"]

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}").text
    assert "瑞幸古茗是比较头部的" in page
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" in page
    assert "古茗店效还行，瑞幸的折扣不能再深了" in page
    assert f'<a class="ocr-link" id="{image_anchor(chat_hash)}"' in page
    assert "原图 1" not in page
    assert f'<img id="{image_anchor(chat_hash)}"' not in page
    assert "<details" not in page
    linked = page.index(f'id="{image_anchor(chat_hash)}"')
    assert linked < page.index("瑞幸和古茗都算头部", linked) < page.index("</a>", linked)
    assert f'<img id="{image_anchor(logo_hash)}"' in page
    assert page.count('class="ocr-shot"') == 1


def test_short_screenshot_post_is_read_by_views_and_digest(conn, settings, tmp_path, monkeypatch):
    image = tmp_path / "chat.png"
    _png(image, b"\x02" * 24)
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-view",
        "图片",
        "茅台是比较头部的。\n\n![](chat.png)\n",
        [image],
    )
    company = conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '贵州茅台'").fetchone()
    mention = conn.execute(
        "SELECT depth FROM article_mentions WHERE article_id = %s AND company_id = %s",
        (row["id"], company["id"]),
    ).fetchone()
    assert mention["depth"] == "passing"
    sources = ["fangwei-zsxq"]
    assert row["id"] not in {item["id"] for item in views_mod.pending_articles(conn, [company["id"]], sources, 20)}

    _remember(conn, row["id"], "chat.png", JUDGMENT)
    refresh_article_ocr(conn, row["id"])
    assert replace_ocr_chunks(conn, row["id"]) >= 1
    conn.commit()
    chunk = conn.execute(
        "SELECT body, anchor FROM article_chunks WHERE article_id = %s AND lang = 'ocr'",
        (row["id"],),
    ).fetchone()
    assert "短期不宜提价" in chunk["body"] and chunk["anchor"].startswith("img-")

    pending = {item["id"] for item in views_mod.pending_articles(conn, [company["id"]], sources, 20)}
    assert row["id"] in pending
    assert row["id"] in {item["id"] for item in views_mod.gap_articles(conn, [company["id"]], sources, 20)}
    assert company["id"] in {item["id"] for item in views_mod.view_companies(conn, sources)}
    companies = views_mod.pending_companies(conn, row["id"])
    assert company["id"] in {item["id"] for item in companies}

    prompts = []

    def capture_view(_settings, messages, **_kwargs):
        prompts.append(messages[1]["content"])
        return (
            '{"贵州茅台": {"about": true, "judgment": "短期不宜提价", "stance": "中性", "quote": "短期不宜提价"}}',
            "grok-test",
        )

    monkeypatch.setattr(views_mod, "chat", capture_view)
    article = conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id FROM articles WHERE id = %s",
        (row["id"],),
    ).fetchone()
    assert views_mod.extract_article(conn, settings, article, "方伟", [company]) == 1
    assert "茅台是比较头部的" in prompts[0]
    assert "【图片里的字】" in prompts[0] and "短期不宜提价" in prompts[0]
    assert "不要因为来自截图就当成没下判断" in prompts[0]
    saved_view = conn.execute(
        "SELECT judgment, ocr_hash FROM company_views WHERE article_id = %s AND company_id = %s",
        (row["id"], company["id"]),
    ).fetchone()
    ocr_hash = conn.execute("SELECT ocr_hash FROM articles WHERE id = %s", (row["id"],)).fetchone()["ocr_hash"]
    assert saved_view["judgment"] == "短期不宜提价" and saved_view["ocr_hash"] == ocr_hash

    conn.execute(
        """
        INSERT INTO article_digests (article_id, content_hash, model, summary, prompt_version, ocr_hash)
        VALUES (%s, %s, 'test', '只写了正文那一句。', %s, '')
        """,
        (row["id"], row["content_hash"], digest_mod.PROMPT_VERSION),
    )
    conn.commit()
    assert row["id"] in {item["id"] for item in digest_mod.pending_articles(conn, sources, 20)}
    digest_row = conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id, ocr_hash FROM articles WHERE id = %s",
        (row["id"],),
    ).fetchone()
    stored = {"prompt_version": digest_mod.PROMPT_VERSION, "head_only": False, "ocr_hash": ""}
    assert digest_mod.digest_due(digest_row, stored)

    seen = []

    def capture_digest(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary": "导读", "points": ["短期不宜提价"], "tags": ["贵州茅台"]}', "grok-test"

    monkeypatch.setattr(digest_mod, "chat", capture_digest)
    digest_mod.digest_article(conn, settings, digest_row, "方伟")
    assert "茅台是比较头部的" in seen[0]
    assert "【图片里的字】" in seen[0] and "短期不宜提价" in seen[0]
    assert "不要只概括正文里那一句" in seen[0]


def test_long_article_passing_mention_is_not_queued_for_a_screenshot(conn, settings, tmp_path):
    image = tmp_path / "chart.png"
    _png(image, b"\x03" * 24)
    filler = "\n\n".join(["今天只是在记盘面，没有要展开的公司。" * 4 for _ in range(8)])
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-long",
        "长文顺带一提",
        filler + "\n\n顺带提一句茅台。\n\n![](chart.png)\n",
        [image],
    )
    _remember(conn, row["id"], "chart.png", JUDGMENT)
    refresh_article_ocr(conn, row["id"])
    conn.commit()
    body_len = conn.execute("SELECT length(body_text) AS n FROM articles WHERE id = %s", (row["id"],)).fetchone()["n"]
    assert body_len >= 400
    company = conn.execute("SELECT id FROM companies WHERE name_zh = '贵州茅台'").fetchone()["id"]
    depth = conn.execute(
        "SELECT depth FROM article_mentions WHERE article_id = %s AND company_id = %s",
        (row["id"], company),
    ).fetchone()["depth"]
    assert depth == "passing"
    assert row["id"] not in {item["id"] for item in views_mod.pending_articles(conn, [company], ["fangwei-zsxq"], 20)}
    assert row["id"] not in {item["id"] for item in views_mod.gap_articles(conn, [company], ["fangwei-zsxq"], 20)}


# 14886：两张重叠的群截图。界面字和别人的名字去掉，方伟 / 网友分块，重复句只留一次。
_SHOT_A = """9:41
100%
5G
微信
瑞幸古茗交流群(86)
方伟Rey
瑞幸和古茗都算头部，但瑞幸同店还有压力
王小明
古茗店效还行，瑞幸的折扣不能再深了
设为群待办
¥9.9
小程序
瑞幸咖啡
李小红
先看到这里，不展开了
"""
_SHOT_B = """20:41
微信
87%
瑞幸古茗交流群(86)
王小明
古茗店效还行，瑞幸的折扣不能再深了
设为群待办
李小红
先看到这里，不展开了
方伟
同店压力还在，折扣不能再深
Rey
会员那边先不动
"""


def test_wechat_dialogue_is_what_the_reader_and_digest_consume(conn, settings, tmp_path, monkeypatch):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    _png(first, b"\x11" * 24)
    _png(second, b"\x12" * 24)
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-14886",
        "图片",
        "瑞幸古茗是比较头部的。\n\n![](a.png)\n\n![](b.png)\n",
        [first, second],
    )
    first_hash = _remember(conn, row["id"], "a.png", _SHOT_A)
    second_hash = _remember(conn, row["id"], "b.png", _SHOT_B)
    refresh_article_ocr(conn, row["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (row["id"],)).fetchone()["ocr_text"]
    assert saved.startswith("方伟：瑞幸和古茗都算头部")
    assert "网友：古茗店效还行" in saved
    assert saved.count("古茗店效还行，瑞幸的折扣不能再深了") == 1
    assert "王小明" not in saved and "李小红" not in saved and "设为群待办" not in saved
    assert "小程序" not in saved and "交流群" not in saved and "¥" not in saved

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}").text
    assert "方伟：" in page and "网友：" in page
    assert page.count("古茗店效还行，瑞幸的折扣不能再深了") == 1
    assert "王小明" not in page and "设为群待办" not in page
    assert f'id="{image_anchor(first_hash)}"' in page and f'id="{image_anchor(second_hash)}"' in page
    assert "原图 1" not in page and "原图 2" not in page
    assert page.count('class="ocr-shot"') == 1
    assert "<details" not in page

    seen = []

    def capture_digest(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary": "导读", "points": ["同店还有压力"], "tags": []}', "grok-test"

    monkeypatch.setattr(digest_mod, "chat", capture_digest)
    digest_row = conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id, ocr_hash FROM articles WHERE id = %s",
        (row["id"],),
    ).fetchone()
    digest_mod.digest_article(conn, settings, digest_row, "方伟")
    assert "【图片里的字】" in seen[0]
    assert "方伟：瑞幸和古茗都算头部" in seen[0]
    assert "网友：古茗店效还行" in seen[0]
    assert "不要把网友的话写成方伟的判断" in seen[0]
    assert "王小明" not in seen[0] and "设为群待办" not in seen[0] and "小程序" not in seen[0]

    prompts = []

    def capture_view(_settings, messages, **_kwargs):
        prompts.append(messages[1]["content"])
        return '{"贵州茅台": null}', "grok-test"

    monkeypatch.setattr(views_mod, "chat", capture_view)
    company = conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '贵州茅台'").fetchone()
    article = conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id FROM articles WHERE id = %s",
        (row["id"],),
    ).fetchone()
    views_mod.extract_article(conn, settings, article, "方伟", [company])
    assert "方伟：瑞幸和古茗都算头部" in prompts[0]
    assert "网友：" in prompts[0]
    assert "不要把网友的话写成方伟的判断" in prompts[0]
    assert "不要因为来自截图就当成没下判断" in prompts[0]
    assert "王小明" not in prompts[0] and "设为群待办" not in prompts[0]


# 文章 8881 的真实识别。旧旁路曾把栏上的「不太看好」整段收成方伟。
_PDD_QUOTE = """拼多多，我是始终不太看好。属于
假本分。个人一家之言仅供参.
方伟看十年作者
零售的本分是帮消费者省钱，
管理层的本分是integrity，世
界范围内在零售行业，做得比
它好的，其实只有Costco，奥
乐齐，利德。
50年下来，沃尔玛的毛利率从
20%提升到了24%，费用率从
16%提升到了20%，只有
Costco50年不变，费用率纹丝
不动，那就是9%，这是零售
领域最强大的公司。
方伟看十年
什么才叫做“拥有一家公司”的感觉？"""


def test_baked_quote_reply_is_resplit_before_views(conn, settings, tmp_path, monkeypatch):
    """观点重抽从 image_texts 重写旁路。库里已经写成「方伟：不太看好」也不会再喂给模型。"""
    image = tmp_path / "pdd.png"
    _png(image, b"\x29" * 24)
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-8881",
        "图片",
        "（见图）\n\n![](pdd.png)\n",
        [image],
    )
    _remember(conn, row["id"], "pdd.png", _PDD_QUOTE)
    conn.execute(
        "UPDATE articles SET ocr_text = %s, ocr_hash = %s WHERE id = %s",
        (
            "方伟：拼多多，我是始终不太看好。属于\n假本分。个人一家之言仅供参.\n零售的本分是帮消费者省钱。",
            "stale",
            row["id"],
        ),
    )
    conn.commit()

    prompts = []

    def capture_view(_settings, messages, **_kwargs):
        prompts.append(messages[1]["content"])
        return (
            '{"拼多多": {"about": true, "judgment": "网友不看好，作者在讲零售的本分", '
            '"stance": "中性", "quote": "零售的本分是帮消费者省钱"}}',
            "grok-test",
        )

    monkeypatch.setattr(views_mod, "chat", capture_view)
    company = conn.execute("SELECT id, name_zh FROM companies WHERE name_zh = '拼多多'").fetchone()
    article = conn.execute(
        "SELECT id, title, body_text, content_hash, published_at, source_id FROM articles WHERE id = %s",
        (row["id"],),
    ).fetchone()
    assert views_mod.extract_article(conn, settings, article, "方伟", [company]) == 1
    prompt = prompts[0]
    assert "【图片里的字】" in prompt
    ocr = prompt.split("【图片里的字】", 1)[1]
    guest, host = ocr.split("方伟：", 1)
    assert "网友：" in guest and "不太看好" in guest
    assert "零售的本分" in host and "不太看好" not in host
    assert "方伟看十年" not in prompt and "什么才叫做" not in prompt
    assert "不要把网友的话写成方伟的判断" in prompt
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (row["id"],)).fetchone()["ocr_text"]
    assert saved.startswith("网友：拼多多，我是始终不太看好")
    assert "方伟：零售的本分是帮消费者省钱" in saved
    view = conn.execute(
        "SELECT judgment, quote FROM company_views WHERE article_id = %s AND company_id = %s",
        (row["id"], company["id"]),
    ).fetchone()
    assert "不太看好" not in view["judgment"] and view["quote"] == "零售的本分是帮消费者省钱"


def test_api_markdown_links_local_images_and_appends_their_text(conn, settings, tmp_path):
    """接口原文：归档里的相对路径换成站内 /media 的完整地址，还没下载的外链图照留；截图里的字附在 .md 最后。"""
    chat = tmp_path / "chat.png"
    _png(chat, b"\x03" * 24)
    row = _store(
        conn,
        settings,
        "fangwei-zsxq",
        "ocr-api",
        "这是好文章",
        "这是好文章\n\n![](../../images/chat.png)\n\n![](https://example.com/far.png)\n",
        [chat],
    )
    _remember(conn, row["id"], "chat.png", CHAT)
    refresh_article_ocr(conn, row["id"])
    replace_ocr_chunks(conn, row["id"])
    conn.commit()
    storage = conn.execute(
        "SELECT storage_path FROM article_images WHERE article_id = %s", (row["id"],)
    ).fetchone()["storage_path"]
    local = f"]({settings.public_base_url}/media/{storage})"

    with TestClient(create_app(settings)) as client:
        data = client.get(f"/api/v1/articles/{row['id']}").json()
        markdown = client.get(f"/api/v1/articles/{row['id']}.md").text
        assert client.get(f"/media/{storage}").status_code == 200
    assert local in data["body_markdown"] and "../../images/" not in data["body_markdown"]
    assert "](https://example.com/far.png)" in data["body_markdown"]
    assert local in markdown
    body, shots = markdown.split("\n## 图片里的字\n", 1)
    assert "瑞幸和古茗都算头部" not in body
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" in shots


def test_search_counts_articles_whose_words_are_only_in_screenshots(conn, settings, tmp_path):
    """只在截图里写了检索词的帖，结果里列着，上面的篇数和左边的来源计数也要数到（以前「送货上门」55 对 16）。"""
    chat = tmp_path / "chat.png"
    _png(chat, b"\x04" * 24)
    shot = _store(conn, settings, "fangwei-zsxq", "ocr-count", "看图", "看图\n\n![](chat.png)\n", [chat])
    _remember(
        conn,
        shot["id"],
        "chat.png",
        "微信\n20:41\n张三\n独有词条丁戊己这个说法是群里先提的，后来大家都这么叫\n李四\n对，独有词条丁戊己确实好记\n",
    )
    refresh_article_ocr(conn, shot["id"])
    replace_ocr_chunks(conn, shot["id"])
    _store(conn, settings, "fangwei-mp", "body-count", "正文里写了", "正文里写了独有词条丁戊己的来历。\n", [])
    conn.commit()

    with TestClient(create_app(settings)) as client:
        page = client.get("/search", params={"q": "独有词条丁戊己"}).text
        api = client.get("/api/v1/search", params={"q": "独有词条丁戊己"}).json()
    assert page.count('href="/articles/') >= 2
    assert '<span>全部来源</span><span class="num">2</span>' in page
    assert '<span>方伟知识星球</span><span class="num">1</span>' in page
    assert "2 篇全部命中" in page
    assert api["total"] == 2 and api["semantic"] == 0

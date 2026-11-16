"""阅读页留原图；摘要和观点的输入仍然读到截图里的字。

样例按 14886 的形状：正文一句，判断在群聊截图里。公司提及表仍不看图片。
识图交回结构的，说话人是模型标好的；全量重认以前存的旧识图（units 为空）照认出来的样子交，不按冒号前的字猜谁说的。
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import ask as ask_mod
from researchlib import digest as digest_mod
from researchlib import views as views_mod
from researchlib.chunks import OCR_HEADING, replace_ocr_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ocr import ocr_speaker_note, refresh_article_ocr
from researchlib.ocr_units import format_units, normalize_units
from researchlib.render import image_anchor
from researchlib.retrieval import retrieve
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.app import create_app
from researchlib.web.queries import Scope

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


def _remember(conn, article_id: int, ref: str, text: str, units: dict | None = None) -> str:
    """存一张图的识别结果。给了 units 是识图交回的结构，字由它排出来；没给的是全量重认以前的旧识图。"""
    row = conn.execute(
        "SELECT content_hash FROM article_images WHERE article_id = %s AND original_ref = %s",
        (article_id, ref),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO image_texts (content_hash, engine, text, units) VALUES (%s, 'test', %s, %s)
        ON CONFLICT (content_hash) DO UPDATE SET text = EXCLUDED.text, units = EXCLUDED.units
        """,
        (row["content_hash"], format_units(units) if units else text, Jsonb(units) if units else None),
    )
    return row["content_hash"]


def _chat(*messages: tuple[str, str]) -> dict:
    """群聊截图的结构：(说话人, 正文)，说话人写「我」的是截图的人自己（靠右的绿气泡）。"""
    return normalize_units(
        {
            "kind": "chat",
            "blocks": [
                {"type": "message", "speaker": "" if who == "我" else who, "self": who == "我", "text": text}
                for who, text in messages
            ],
        }
    )


def test_text_without_structure_reaches_models_as_recognized(conn, settings, tmp_path, monkeypatch):
    """旧识图只有一段自由文字：旁路、检索块、摘要的输入都照认出来的样子，不拆说话人，也不补说话人说明。

    只有一处例外：行首写着「方伟：」的整行加「」。自由文字里的这个冒号没经过识图结构核对，
    不能和结构标好的作者的话一个样（审核 C5）；字一个不少。
    """
    image = tmp_path / "metrics.png"
    _png(image, b"\x19" * 24)
    article = _store(
        conn, settings, "fangwei-zsxq", "synthetic-field-labels", "合成财务讨论",
        "本篇讨论经营情况。\n\n![](metrics.png)\n", [image],
    )
    raw = (
        "方伟：先看经营情况 营收：增长百分之二十 净利润：增长百分之十 假设：需求不变 "
        "净资产：123.45亿元 净资产收益率：10.5% 资本开支：-2.5亿元\n"
        "云间拾光客：继续核对现金流 方伟：再看下一季度"
    )
    digest = _remember(conn, article["id"], "metrics.png", raw)
    refresh_article_ocr(conn, article["id"])
    assert replace_ocr_chunks(conn, article["id"]) == 1
    conn.commit()
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id=%s", (article["id"],)).fetchone()["ocr_text"]
    first, second = raw.split("\n")
    assert saved == f"「{first}」\n{second}"
    chunk = conn.execute(
        "SELECT heading,body,anchor FROM article_chunks WHERE article_id=%s AND lang='ocr'", (article["id"],),
    ).fetchone()
    assert chunk == {"heading": OCR_HEADING, "body": saved, "anchor": image_anchor(digest)}
    assert ocr_speaker_note(conn, article["id"], saved, "方伟") == ""
    seen = []

    def fake_chat(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary":"合成导读","points":[],"tags":[]}', "synthetic"

    monkeypatch.setattr(digest_mod, "chat", fake_chat)
    row = conn.execute(
        "SELECT id,title,body_text,content_hash,published_at,source_id,ocr_hash FROM articles WHERE id=%s",
        (article["id"],),
    ).fetchone()
    digest_mod.digest_article(conn, settings, row, "方伟")
    assert seen and saved in seen[0] and "别人的话不要写成" not in seen[0]
    assert conn.execute("SELECT text FROM image_texts WHERE content_hash=%s", (digest,)).fetchone()["text"] == raw
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article['id']}").text
    prose = page.split('class="prose"', 1)[1].split("</article>", 1)[0]
    assert f'<img id="{image_anchor(digest)}"' in prose
    assert "ocr-reading" not in prose and "增长百分之二十" not in prose


@pytest.mark.parametrize("label", ["Alice", "NewRatio", "Al", "O'Brien", "云间拾光客"])
def test_model_labelled_speakers_reach_chunks_and_prompts_as_labelled(conn, settings, tmp_path, monkeypatch, label):
    """说话人是识图看着图标的：少见的网名、像指标的英文名都照标的写，不再按冒号前的字形猜。"""
    image = tmp_path / "latin.png"
    _png(image, b"\x39" * 24)
    article = _store(
        conn, settings, "fangwei-zsxq", "synthetic-latin-label", "合成英文昵称边界",
        "本篇记录不同人的判断。\n\n![](latin.png)\n", [image],
    )
    other = f"{label}：我认为可以提价，营收：增长百分之十。"
    units = _chat(("我", "我的判断是先观察，暂时不下结论。"), (label, "我认为可以提价，营收：增长百分之十。"))
    _remember(conn, article["id"], "latin.png", "", units)
    refresh_article_ocr(conn, article["id"])
    replace_ocr_chunks(conn, article["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id=%s", (article["id"],)).fetchone()["ocr_text"]
    assert saved == f"方伟：我的判断是先观察，暂时不下结论。\n\n{other}"
    chunk = conn.execute(
        "SELECT body,heading FROM article_chunks WHERE article_id=%s AND lang='ocr'", (article["id"],),
    ).fetchone()
    assert chunk == {"heading": OCR_HEADING, "body": f"方伟：我的判断是先观察，暂时不下结论。\n{other}"}
    sources = enabled_sources(load_sources(settings.sources_path))
    found = retrieve(conn, "概括这篇", Scope(source_ids=["fangwei-zsxq"], article_id=article["id"]), sources)
    assert other in ask_mod._material(ask_mod._passages(found, sources, settings))
    # 摘要真实入口使用替身，只检查实际送出的资料与说话人说明，不请求任何模型。
    seen = []

    def fake_chat(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary":"合成导读","points":[],"tags":[]}', "synthetic"

    monkeypatch.setattr(digest_mod, "chat", fake_chat)
    row = conn.execute(
        "SELECT id,title,body_text,content_hash,published_at,source_id,ocr_hash FROM articles WHERE id=%s",
        (article["id"],),
    ).fetchone()
    digest_mod.digest_article(conn, settings, row, "方伟")
    assert seen and other in seen[0] and "别人的话不要写成方伟的判断" in seen[0]


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
    prose = page.split('class="prose"', 1)[1].split("</article>", 1)[0]
    assert "瑞幸古茗是比较头部的" in prose
    assert "瑞幸和古茗都算头部，但瑞幸同店还有压力" not in prose
    assert "古茗店效还行" not in prose
    assert "ocr-link" not in prose and "ocr-reading" not in prose and "ocr-shot" not in prose
    assert f'<img id="{image_anchor(chat_hash)}"' in prose
    assert f'<img id="{image_anchor(logo_hash)}"' in prose
    assert "原图 1" not in prose


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
    assert [item["id"] for item in digest_mod.rows_needing_digest(conn, [row["id"]])] == [row["id"]]

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


# 14886：两张重叠的群截图，识图交回的结构。后一张开头两条和前一张结尾重合。昵称和对话都是编的。
_SHOT_A = _chat(
    ("我", "瑞幸和古茗都算头部，但瑞幸同店还有压力"),
    ("王小明", "古茗店效还行，瑞幸的折扣不能再深了"),
    ("李小红", "先看到这里，不展开了"),
)
_SHOT_B = _chat(
    ("王小明", "古茗店效还行，瑞幸的折扣不能再深了"),
    ("李小红", "先看到这里，不展开了"),
    ("我", "同店压力还在，折扣不能再深"),
    ("我", "会员那边先不动"),
)


def test_screenshot_dialogue_is_what_the_digest_and_views_read(conn, settings, tmp_path, monkeypatch):
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
    first_hash = _remember(conn, row["id"], "a.png", "", _SHOT_A)
    second_hash = _remember(conn, row["id"], "b.png", "", _SHOT_B)
    refresh_article_ocr(conn, row["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (row["id"],)).fetchone()["ocr_text"]
    assert saved.startswith("方伟：瑞幸和古茗都算头部")
    assert "王小明：古茗店效还行" in saved
    assert saved.count("古茗店效还行，瑞幸的折扣不能再深了") == 1
    assert "李小红：先看到这里" in saved
    assert saved.endswith("方伟：同店压力还在，折扣不能再深\n会员那边先不动")

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}").text
    prose = page.split('class="prose"', 1)[1].split("</article>", 1)[0]
    assert "瑞幸古茗是比较头部的" in prose
    assert "方伟：" not in prose and "王小明：" not in prose
    assert "古茗店效还行" not in prose
    assert "ocr-link" not in prose and "ocr-reading" not in prose
    assert f'<img id="{image_anchor(first_hash)}"' in prose
    assert f'<img id="{image_anchor(second_hash)}"' in prose

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
    assert "王小明：古茗店效还行" in seen[0]
    assert "别人的话不要写成方伟的判断" in seen[0]

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
    assert "王小明：" in prompts[0]
    assert "别人的话不要写成方伟的判断" in prompts[0]
    assert "不要因为来自截图就当成没下判断" in prompts[0]


# 文章 8881 的形状：方伟引用一位网友的帖子回复。识图把被引用的那条标在 quote 里，不是方伟说的。
_PDD_QUOTE = normalize_units(
    {
        "kind": "post",
        "blocks": [
            {
                "type": "message",
                "self": True,
                "text": "零售的本分是帮消费者省钱，管理层的本分是integrity，世界范围内在零售行业，"
                "做得比它好的，其实只有Costco，奥乐齐，利德。",
                "quote": {"speaker": "", "text": "拼多多，我是始终不太看好。属于假本分。个人一家之言仅供参."},
            }
        ],
    }
)


def test_baked_quote_reply_is_resplit_before_views(conn, settings, tmp_path, monkeypatch):
    """观点重抽从 image_texts 重写旁路。库里旧的那份写成了「方伟：不太看好」，也不会再喂给模型。"""
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
    _remember(conn, row["id"], "pdd.png", "", _PDD_QUOTE)
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
    assert "别人的话不要写成方伟的判断" in prompt
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


def test_search_counts_a_post_whose_words_are_split_between_text_and_screenshot(conn, settings, tmp_path):
    """一个词在正文、另一个只在截图里：列表算全部命中；文章级索引要全部词落在同一处，数不到，来源计数按列表补上。"""
    chat = tmp_path / "chat.png"
    _png(chat, b"\x05" * 24)
    body = "冷链仓配这块先不展开。\n\n![](chat.png)\n"
    shot = _store(conn, settings, "fangwei-zsxq", "ocr-split", "看图", body, [chat])
    _remember(
        conn,
        shot["id"],
        "chat.png",
        "微信\n20:41\n张三\n他们现在主要靠送货上门拉复购\n李四\n对，送货上门的成本也不低\n",
    )
    refresh_article_ocr(conn, shot["id"])
    replace_ocr_chunks(conn, shot["id"])
    conn.commit()

    with TestClient(create_app(settings)) as client:
        page = client.get("/search", params={"q": "冷链仓配 送货上门"}).text
    assert f'href="/articles/{shot["id"]}?' in page
    assert "1 篇全部命中" in page
    assert '<span>全部来源</span><span class="num">1</span>' in page
    assert '<span>方伟知识星球</span><span class="num">1</span>' in page


def test_only_estimated_wechat_images_move_beside_matching_paragraphs(conn, settings, tmp_path):
    """按识图挪图只给极致了估位置的公众号正文用。飞书的表图跟在介绍它的那一段后面，
    和下一节的一段同有一个更长的词也不挪；文章页和飞书目录右侧一样。"""
    body = (
        "## 一、利润表\n\n"
        "先看2023年的利润表，下图是年报里的合并利润表：\n\n"
        "![](table.png)\n\n"
        "收入增长主要来自海外市场，国内基本持平。\n\n"
        "## 二、怎么看利润\n\n"
        "归属于母公司股东的净利润同比增长了二成，主要是费用率下降。\n"
    )
    shot = "合并利润表\n项目 2023年 2022年\n营业总收入 1,234 1,100\n归属于母公司股东的净利润 300 250\n"
    stored = {}
    for number, source_id in enumerate(("linwenfeng-feishu", "fangwei-mp")):
        folder = tmp_path / source_id
        folder.mkdir()
        _png(folder / "table.png", bytes([0x30 + number]) * 24)
        row = _store(conn, settings, source_id, f"synthetic-table-{source_id}", "合成利润表", body,
                     [folder / "table.png"])
        stored[source_id] = (row["id"], _remember(conn, row["id"], "table.png", shot))
    feishu_id = stored["linwenfeng-feishu"][0]
    conn.execute(
        "INSERT INTO doc_tree (source_id, article_id, parent_id, ord) VALUES ('linwenfeng-feishu', %s, NULL, 0)",
        (feishu_id,),
    )
    conn.commit()

    def order(html: str, digest: str) -> list[str]:
        marks = {"intro": "下图是年报里的合并利润表", "image": f'<img id="{image_anchor(digest)}"',
                 "next": "收入增长主要来自海外市场", "later": "归属于母公司股东的净利润同比增长"}
        return sorted(marks, key=lambda name: html.index(marks[name]))

    with TestClient(create_app(settings)) as client:
        pages = {source_id: client.get(f"/articles/{article_id}").text
                 for source_id, (article_id, _digest) in stored.items()}
        tree = client.get(f"/sources/linwenfeng-feishu/tree/{feishu_id}").text
    feishu_digest = stored["linwenfeng-feishu"][1]
    assert order(pages["linwenfeng-feishu"], feishu_digest) == ["intro", "image", "next", "later"]
    assert order(tree, feishu_digest) == ["intro", "image", "next", "later"]
    # 方伟公众号的图是极致了估的位置，照旧挪到对得上的那一段前面。
    assert order(pages["fangwei-mp"], stored["fangwei-mp"][1]) == ["intro", "next", "image", "later"]


def test_wechat_archive_from_the_original_page_keeps_images_where_they_are(conn, settings, tmp_path):
    """正文从公众号原文页取的方伟公众号归档（归档头「- 图片位置：原文页」）：图就在原文的位置，阅读页不按识图挪。
    这一行和别的归档头一样不显示，不进正文文字。"""
    from researchlib.ingest.parse import parse_archive

    archive = tmp_path / "fangwei-mp"
    (archive / "md" / "2026").mkdir(parents=True)
    _png(archive / "md" / "2026" / "table.png", b"\x41" * 24)
    sn = "d" * 32
    (archive / "md" / "2026" / f"2026-11-07-{sn}.md").write_text(
        "# 合成利润表\n\n- 作者：方伟\n- 时间：2026-11-07 09:00:00 (UTC+8)\n"
        f"- sn：`{sn}`\n- 原链接：https://mp.weixin.qq.com/s?sn={sn}\n"
        f"- 原始数据：[raw/articles/{sn}.json](../../raw/articles/{sn}.json)\n- 图片位置：原文页\n\n"
        "先看2023年的利润表，下图是年报里的合并利润表：\n\n![](table.png)\n\n"
        "收入增长主要来自海外市场，国内基本持平。\n\n"
        "归属于母公司股东的净利润同比增长了二成，主要是费用率下降。\n",
        encoding="utf-8",
    )
    docs, errors, files, images = parse_archive(archive, tmp_path / "work", id_mode="path")
    assert errors == [] and docs[0].raw_meta["images"] == "page"
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-mp")
    store_documents(conn, source=source, docs=docs, data_dir=settings.data_dir, archive_files=files,
                    images_in_archive=images, errors=[])
    row = conn.execute(
        "SELECT id, body_text, raw_meta FROM articles WHERE source_id = 'fangwei-mp' AND external_id = %s",
        (f"md/2026/2026-11-07-{sn}.md",),
    ).fetchone()
    assert row["raw_meta"]["images"] == "page" and "图片位置" not in row["body_text"]
    digest = _remember(
        conn, row["id"], "table.png",
        "合并利润表\n项目 2023年 2022年\n营业总收入 1,234 1,100\n归属于母公司股东的净利润 300 250\n",
    )
    conn.commit()

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}").text
    marks = ("下图是年报里的合并利润表", f'<img id="{image_anchor(digest)}"', "收入增长主要来自海外市场",
             "归属于母公司股东的净利润同比增长")
    assert [page.index(mark) for mark in marks] == sorted(page.index(mark) for mark in marks)
    assert "图片位置" not in page

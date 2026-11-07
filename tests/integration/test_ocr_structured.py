"""识图交回结构以后：旁路正文、检索块、摘要观点的提示、引用条都按模型标好的说话人走，不再过旧规则。

旧格式（units 为空）的图照旧由 wechat_dialogue 整理，见 test_ocr_reading.py。
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import digest as digest_mod
from researchlib.chunks import OCR_HEADING, OCR_ORD_BASE, rebuild_ocr_chunks, replace_ocr_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.ocr import Recognition, article_ocr, ocr_speaker_note, refresh_article_ocr, run_ocr
from researchlib.ocr_units import format_units, normalize_units
from researchlib.render import image_anchor
from researchlib.sources import load_sources
from researchlib.web.app import create_app
from researchlib.wechat_dialogue import attribution_uncertain

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _msg(text: str, speaker: str = "", *, me: bool = False, cut: str = "") -> dict:
    block = {"type": "message", "speaker": speaker, "self": me, "text": text}
    if cut:
        block["cut"] = cut
    return block


def _units(*blocks: dict, kind: str = "chat") -> dict:
    return normalize_units({"kind": kind, "blocks": list(blocks)})


# 冒号前是少见的网名、正文里也有冒号：旧规则会把它当成「说话人边界未核实」。结构里说话人是识图标好的。
CHAT = _units(
    _msg("毛利率：35%，比去年高", "王小明"),
    _msg("先看现金流，负债率也要看", me=True),
    _msg("注意：明天出财报，等数据再说", "股海老船长2号"),
)


def _png(path: Path, marker: bytes) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker)


def _store(conn, settings, source_id: str, external_id: str, body: str, images: list[Path]):
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    doc = ParsedDoc(
        external_id=external_id,
        title="截图",
        published_at=datetime(2026, 11, 7, 9, 0, tzinfo=SHANGHAI),
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
        "SELECT id, title, body_text, content_hash, published_at, source_id, ocr_hash"
        " FROM articles WHERE external_id = %s AND source_id = %s",
        (external_id, source_id),
    ).fetchone()


def _remember(conn, article_id: int, ref: str, units: dict | None, text: str = "") -> str:
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


def test_structured_chat_reaches_text_chunks_and_prompts_as_the_model_labelled_it(conn, settings, tmp_path):
    image = tmp_path / "chat.png"
    _png(image, b"\x51" * 24)
    article = _store(conn, settings, "fangwei-zsxq", "structured-chat", "正文一句。\n\n![](chat.png)\n", [image])
    digest = _remember(conn, article["id"], "chat.png", CHAT)
    refresh_article_ocr(conn, article["id"])
    assert replace_ocr_chunks(conn, article["id"]) == 1
    conn.commit()

    expected = (
        "王小明：毛利率：35%，比去年高\n\n方伟：先看现金流，负债率也要看\n\n股海老船长2号：注意：明天出财报，等数据再说"
    )
    saved = conn.execute("SELECT ocr_text, ocr_hash FROM articles WHERE id = %s", (article["id"],)).fetchone()
    assert saved["ocr_text"] == expected
    # 旧规则会把这段判成「说话人边界未核实」；结构化的图不按冒号猜，检索块和提示都不带这个警示。
    assert attribution_uncertain(expected)
    chunk = conn.execute(
        "SELECT ord, heading, body, anchor FROM article_chunks WHERE article_id = %s AND lang = 'ocr'",
        (article["id"],),
    ).fetchone()
    assert chunk == {
        "ord": OCR_ORD_BASE,
        "heading": OCR_HEADING,
        "body": (
            "王小明：毛利率：35%，比去年高\n方伟：先看现金流，负债率也要看\n股海老船长2号：注意：明天出财报，等数据再说"
        ),
        "anchor": image_anchor(digest),
    }
    # 摘要和观点读到的就是这一份，不再拿旧规则过一遍（旧规则会给方伟那段补句号）。
    assert article_ocr(conn, article["id"]) == (expected, saved["ocr_hash"])
    note = ocr_speaker_note(conn, article["id"], expected, "方伟")
    assert "截图里的对话已整理成「说话人：正文」" in note and "尚未确认" not in note
    # 维护命令按同一套重切，结果一样，不算变了。
    assert rebuild_ocr_chunks(conn, dry_run=True)[1] == 0

    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{article['id']}", params={"chunk": chunk["ord"]}).text
    quoted = page.split('class="quoted"', 1)[1].split("</aside>", 1)[0]
    assert "方伟：先看现金流，负债率也要看\n股海老船长2号：注意：明天出财报，等数据再说" in quoted


def test_other_sources_name_their_author_in_chunks_and_digest(conn, settings, tmp_path, monkeypatch):
    image = tmp_path / "chat.png"
    _png(image, b"\x52" * 24)
    article = _store(conn, settings, "chengfengpolang", "structured-host", "看图。\n\n![](chat.png)\n", [image])
    _remember(conn, article["id"], "chat.png", CHAT)
    refresh_article_ocr(conn, article["id"])
    replace_ocr_chunks(conn, article["id"])
    conn.commit()
    body = conn.execute(
        "SELECT body FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article["id"],)
    ).fetchone()["body"]
    assert "乘风破浪：先看现金流" in body and "方伟" not in body

    seen = []

    def capture(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary": "导读", "points": ["先看现金流"], "tags": []}', "test"

    monkeypatch.setattr(digest_mod, "chat", capture)
    digest_mod.digest_article(conn, settings, article, "乘风破浪")
    assert "乘风破浪：先看现金流，负债率也要看" in seen[0]
    assert "绿气泡是这篇的作者乘风破浪" in seen[0]
    assert "尚未确认其说话人边界" not in seen[0]


def test_author_account_in_a_post_screenshot_is_the_author(conn, settings, tmp_path):
    """星球帖子、评论截图没有左右气泡，识图都标 self=false。显示名是来源配置里作者本人账号的，写成作者。"""
    image = tmp_path / "post.png"
    _png(image, b"\x58" * 24)
    article = _store(conn, settings, "chengfengpolang", "structured-owner", "看图。\n\n![](post.png)\n", [image])
    digest = _remember(
        conn,
        article["id"],
        "post.png",
        _units(
            _msg("这家公司的现金流一直很稳，负债也低", "王小明"),
            _msg("同意，但估值已经不便宜了，先等一等", "郑立涛"),
            kind="post",
        ),
    )
    refresh_article_ocr(conn, article["id"])
    replace_ocr_chunks(conn, article["id"])
    conn.commit()
    saved = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (article["id"],)).fetchone()["ocr_text"]
    assert saved == "王小明：这家公司的现金流一直很稳，负债也低\n\n方伟：同意，但估值已经不便宜了，先等一等"
    body = conn.execute(
        "SELECT body FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article["id"],)
    ).fetchone()["body"]
    assert "乘风破浪：同意，但估值已经不便宜了" in body and "郑立涛" not in body
    # 单张图的字不知道来源，照图上的名字存。
    stored = conn.execute("SELECT text FROM image_texts WHERE content_hash = %s", (digest,)).fetchone()["text"]
    assert "郑立涛：同意" in stored
    assert rebuild_ocr_chunks(conn, dry_run=True)[1] == 0


def test_overlapping_screenshots_are_stitched_in_text_and_chunks(conn, settings, tmp_path):
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    _png(first, b"\x53" * 24)
    _png(second, b"\x54" * 24)
    article = _store(
        conn, settings, "fangwei-zsxq", "structured-stitch", "两张截图。\n\n![](a.png)\n\n![](b.png)\n", [first, second]
    )
    _remember(
        conn, article["id"], "a.png",
        _units(_msg("茅台批价这一周没动，渠道仍在观望", "张三"), _msg("短期不宜提", "李四", cut="bottom")),
    )
    _remember(
        conn, article["id"], "b.png",
        _units(
            _msg("短期不宜提价，等中秋旺季过了再看", "李四"),
            _msg("同意，先看经销商库存和渠道的反馈，下周再看一次批价", me=True),
        ),
    )
    refresh_article_ocr(conn, article["id"])
    assert replace_ocr_chunks(conn, article["id"]) == 2
    conn.commit()
    text = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (article["id"],)).fetchone()["ocr_text"]
    assert text == (
        "张三：茅台批价这一周没动，渠道仍在观望\n\n李四：短期不宜提价，等中秋旺季过了再看\n\n"
        "方伟：同意，先看经销商库存和渠道的反馈，下周再看一次批价"
    )
    bodies = [
        row["body"]
        for row in conn.execute(
            "SELECT body FROM article_chunks WHERE article_id = %s AND lang = 'ocr' ORDER BY ord", (article["id"],)
        ).fetchall()
    ]
    assert bodies == [
        "张三：茅台批价这一周没动，渠道仍在观望\n李四：短期不宜提价，等中秋旺季过了再看",
        "方伟：同意，先看经销商库存和渠道的反馈，下周再看一次批价",
    ]


def test_legacy_and_structured_images_keep_their_order(conn, settings, tmp_path):
    old, new = tmp_path / "old.png", tmp_path / "new.png"
    _png(old, b"\x55" * 24)
    _png(new, b"\x56" * 24)
    article = _store(
        conn, settings, "fangwei-zsxq", "structured-mixed", "新旧两张。\n\n![](old.png)\n\n![](new.png)\n", [old, new]
    )
    _remember(conn, article["id"], "old.png", None, "方伟：旧图里的判断是渠道库存偏高\n网友：那就再等等吧")
    _remember(conn, article["id"], "new.png", CHAT)
    refresh_article_ocr(conn, article["id"])
    conn.commit()
    text = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (article["id"],)).fetchone()["ocr_text"]
    assert text.index("渠道库存偏高") < text.index("王小明：毛利率：35%，比去年高")
    assert text.endswith("股海老船长2号：注意：明天出财报，等数据再说")


def test_run_ocr_stores_the_structure_and_usage(conn, settings, tmp_path):
    image = tmp_path / "chat.png"
    _png(image, b"\x57" * 24)
    article = _store(conn, settings, "fangwei-zsxq", "structured-run", "正文一句。\n\n![](chat.png)\n", [image])

    def recognize(_path):
        return Recognition(format_units(CHAT), CHAT, {"prompt": 2000, "completion": 6000, "reasoning": 5000})

    report = run_ocr(conn, settings.data_dir, 10, recognize)
    assert (report["images"], report["structured"], report["tokens"]["completion"]) == (1, 1, 6000)
    row = conn.execute("SELECT engine, text, units, usage FROM image_texts").fetchone()
    assert row["units"] == CHAT and row["usage"] == {"prompt": 2000, "completion": 6000, "reasoning": 5000}
    assert row["text"].startswith("王小明：毛利率：35%")
    text = conn.execute("SELECT ocr_text FROM articles WHERE id = %s", (article["id"],)).fetchone()["ocr_text"]
    assert text == format_units(CHAT)
    heading = conn.execute(
        "SELECT heading FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article["id"],)
    ).fetchone()["heading"]
    assert heading == OCR_HEADING

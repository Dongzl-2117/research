"""全库重认（不点名文章的 research-ocr --redo）停了再开：按现在的说明认过的图不再认，不再付一次钱。

以前 --redo 每次都从最新的图从头认，11-08 线上重开一轮 2 万张的重认，停下再开就会把刚认过的再认一遍。
"""

from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.migrate import split_sql
from researchlib.ocr import VISION_PROMPT_VERSION, Recognition, pending_images, redo_remaining, run_ocr
from researchlib.ocr_units import plain_units

BODY = "茅台三季度利润还在涨，飞天批价跌了但渠道库存不高，系列酒拖累了收入。"
MESSAGE = {"type": "message", "speaker": "王小明", "self": False, "text": "毛利率：35%"}
CHAT = {"v": 1, "kind": "chat", "blocks": [MESSAGE]}
MIGRATION = Path(__file__).resolve().parents[2] / "src/researchlib/migrations/032_image_text_prompt_version.sql"


def _articles(conn) -> dict[str, int]:
    conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)
        VALUES ('fangwei-mp', 'new', 'n', '新的一篇', '2026-11-07 12:00+08', %(body)s, %(body)s),
               ('fangwei-mp', 'old', 'o', '旧的一篇', '2026-10-01 12:00+08', %(body)s, %(body)s)
        """,
        {"body": BODY},
    )
    return {row["external_id"]: row["id"] for row in conn.execute("SELECT id, external_id FROM articles").fetchall()}


def _image(conn, external_id: str, digit: str, data_dir: Path | None = None) -> str:
    content_hash = digit * 32
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        SELECT id, source_id, %s, %s, %s FROM articles WHERE external_id = %s
        """,
        (f"{digit}.png", f"x/{digit}.png", content_hash, external_id),
    )
    if data_dir is not None:
        path = data_dir / "images" / "x" / f"{digit}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + digit.encode())
    return content_hash


def _text(conn, content_hash: str, engine: str, text: str, version: int | None, units: dict | None = None):
    conn.execute(
        "INSERT INTO image_texts (content_hash, engine, text, units, prompt_version) VALUES (%s, %s, %s, %s, %s)",
        (content_hash, engine, text, Jsonb(units) if units else None, version),
    )


def test_bulk_redo_skips_images_already_read_with_the_current_prompt(conn, settings):
    ids = _articles(conn)
    data_dir = Path(settings.data_dir)
    old_chat = _image(conn, "new", "a", data_dir)  # 以前的说明认的，有字
    current = _image(conn, "new", "b", data_dir)  # 现在的说明认过
    failed = _image(conn, "old", "c", data_dir)  # 现在的说明认失败了
    invalid = _image(conn, "old", "d", data_dir)  # 现在的说明认了，结构读不出来
    never = _image(conn, "old", "e", data_dir)  # 从没认过
    blank = _image(conn, "old", "f", data_dir)  # 以前的说明认的，没认出字
    older = _image(conn, "old", "g", data_dir)  # 上一版说明认的结构（说明改了版本，要按新说明重认）
    grok_current = _image(conn, "old", "h", data_dir)  # 第 3 版、engine 已是 grok，不能因为换模型再认
    free_text = _image(conn, "old", "i", data_dir)  # 第 3 版认了，模型交回自由文字，#316 以前没存结构
    plain = _image(conn, "old", "j", data_dir)  # 第 3 版认了、交回自由文字，#316 以后存成几段文字
    empty = _image(conn, "old", "k", data_dir)  # 第 3 版认了，没认出字（照片、装饰图）
    _text(conn, old_chat, "deepseek-flash-vision", "王小明：毛利率：35%", None)
    _text(conn, current, "deepseek-flash-vision", "王小明：毛利率：35%", VISION_PROMPT_VERSION, CHAT)
    _text(conn, grok_current, "grok-4.7-vision", "王小明：毛利率：35%", VISION_PROMPT_VERSION, CHAT)
    _text(conn, failed, "deepseek-flash-vision:error", "", VISION_PROMPT_VERSION)
    _text(conn, invalid, "deepseek-flash-vision:invalid", "", VISION_PROMPT_VERSION)
    _text(conn, blank, "deepseek-flash-vision", "", None)
    _text(conn, older, "deepseek-flash-vision", "王小明：毛利率：35%", VISION_PROMPT_VERSION - 1, CHAT)
    _text(conn, free_text, "deepseek-flash-vision", "王小明：毛利率：35%", VISION_PROMPT_VERSION)
    _text(conn, plain, "deepseek-flash-vision", "毛利率：35%", VISION_PROMPT_VERSION, plain_units("毛利率：35%"))
    _text(conn, empty, "deepseek-flash-vision", "", VISION_PROMPT_VERSION)
    conn.commit()

    def picked(**kwargs) -> list[str]:
        return [row["content_hash"] for row in pending_images(conn, 10, **kwargs)]

    bulk = picked(redo=True)
    # 旧识图的整理规则删了（#316）：按现在的说明认了却没存结构、有字的，也重认一次。
    assert set(bulk) == {old_chat, failed, never, blank, older, free_text}
    assert bulk[0] == old_chat  # 新文章的图先认
    assert redo_remaining(conn) == {"images": 6, "with_text": 3}
    # 点名的照旧全部重认（:invalid 靠它点名重认）；不带 --redo 只认没认过的和失败的。
    assert set(picked(redo=True, article_ids=[ids["old"]])) == {
        failed, invalid, never, blank, older, grok_current, free_text, plain, empty,
    }
    assert set(picked()) == {failed, never}

    # 认一张就停，再开从没认完的接着认。
    seen: list[str] = []

    def recognize(path: Path) -> Recognition:
        seen.append(path.name)
        return Recognition("王小明：毛利率：35%", CHAT, {"prompt": 10, "completion": 20})

    report = run_ocr(conn, data_dir, 1, recognize, engine="deepseek-flash-vision", redo=True)
    assert report["images"] == 1 and seen == ["a.png"]
    row = conn.execute("SELECT prompt_version, usage FROM image_texts WHERE content_hash = %s", (old_chat,)).fetchone()
    assert row["prompt_version"] == VISION_PROMPT_VERSION and row["usage"] == {"prompt": 10, "completion": 20}
    assert set(picked(redo=True)) == {failed, never, blank, older, free_text}
    assert redo_remaining(conn) == {"images": 5, "with_text": 2}


def test_failed_redo_keeps_the_old_text_and_stays_pending(conn, settings):
    """重认失败不拿空正文盖掉旧字；版本不动，下次 --redo 还会挑它。以前一失败旧字就没了。"""
    _articles(conn)
    data_dir = Path(settings.data_dir)
    old_chat = _image(conn, "new", "a", data_dir)
    _text(conn, old_chat, "deepseek-flash-vision", "王小明：毛利率：35%", None)
    conn.commit()

    def broken(_path: Path) -> Recognition:
        raise RuntimeError("连不上")

    report = run_ocr(conn, data_dir, 5, broken, engine="deepseek-flash-vision", redo=True)
    assert (report["images"], report["failed"], report["kept"]) == (1, 1, 1)
    row = conn.execute(
        "SELECT engine, text, prompt_version FROM image_texts WHERE content_hash = %s", (old_chat,)
    ).fetchone()
    assert row["engine"] == "deepseek-flash-vision" and row["text"] == "王小明：毛利率：35%"
    assert row["prompt_version"] is None
    assert [found["content_hash"] for found in pending_images(conn, 10, redo=True)] == [old_chat]


def test_migration_marks_rows_written_since_structured_reading(conn):
    """031 以后认的行（带结构、带用量、结构读不出来）算第 1 版；以前的留空，全库重认会认它们。重复跑不改填过的。"""
    _articles(conn)
    structured = _image(conn, "new", "a")
    metered = _image(conn, "new", "b")
    unreadable = _image(conn, "new", "c")
    old = _image(conn, "old", "d")
    conn.execute(
        """
        INSERT INTO image_texts (content_hash, engine, text, units, usage) VALUES
          (%s, 'deepseek-flash-vision', '王小明：你好', '{"v": 1, "kind": "chat", "blocks": []}', '{"prompt": 1}'),
          (%s, 'deepseek-flash-vision', '自由文字', NULL, '{"prompt": 1}'),
          (%s, 'deepseek-flash-vision:invalid', '', NULL, NULL),
          (%s, 'deepseek-flash-vision', '旧的字', NULL, NULL)
        """,
        (structured, metered, unreadable, old),
    )
    for _ in range(2):
        for statement in split_sql(MIGRATION.read_text(encoding="utf-8")):
            conn.execute(statement)
    versions = {
        row["content_hash"]: row["prompt_version"]
        for row in conn.execute("SELECT content_hash, prompt_version FROM image_texts").fetchall()
    }
    assert versions == {structured: 1, metered: 1, unreadable: 1, old: None}

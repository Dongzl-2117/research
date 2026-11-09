"""research-ocr --tall：只挑以前整张认的长截图切段重认，认过的不再付一次钱，失败留着旧字。

长图切段以前，整张压到长边 1920 像素，1080×7237 的长截图送出去宽只剩 286，认出来的字错得多、改了意思。
"""

from pathlib import Path

from PIL import Image
from psycopg.types.json import Jsonb

from researchlib.ocr import (
    VISION_PROMPT_VERSION,
    Recognition,
    pending_images,
    pending_tall_images,
    run_ocr,
    tall_remaining,
    tile_count,
)

BODY = "茅台三季度利润还在涨，飞天批价跌了但渠道库存不高，系列酒拖累了收入。"
MESSAGE = {"type": "message", "speaker": "王小明", "self": False, "text": "毛利率35%"}
CHAT = {"v": 1, "kind": "chat", "blocks": [MESSAGE]}
TILED = {**CHAT, "tiles": 2}


def _articles(conn) -> None:
    conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)
        VALUES ('fangwei-mp', 'new', 'n', '新的一篇', '2026-11-07 12:00+08', %(body)s, %(body)s),
               ('fangwei-mp', 'old', 'o', '旧的一篇', '2026-10-01 12:00+08', %(body)s, %(body)s)
        """,
        {"body": BODY},
    )


def _image(conn, data_dir: Path, external_id: str, digit: str, size: tuple[int, int]) -> str:
    content_hash = digit * 32
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        SELECT id, source_id, %s, %s, %s FROM articles WHERE external_id = %s
        """,
        (f"{digit}.png", f"x/{digit}.png", content_hash, external_id),
    )
    path = data_dir / "images" / "x" / f"{digit}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)
    return content_hash


def _text(conn, content_hash: str, engine: str, text: str, version: int | None, units: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO image_texts (content_hash, engine, text, units, prompt_version) VALUES (%s, %s, %s, %s, %s)",
        (content_hash, engine, text, Jsonb(units) if units else None, version),
    )


def test_tall_redo_picks_only_long_shots_read_whole_with_the_current_prompt(conn, settings):
    _articles(conn)
    data_dir = Path(settings.data_dir)
    tall, phone = (600, 2400), (600, 1200)
    whole = _image(conn, data_dir, "new", "a", tall)  # 现在的说明整张认的长图：要切段重认
    unreadable = _image(conn, data_dir, "old", "b", tall)  # 整张认，结构读不出来：切段常常就认得出来
    tiled = _image(conn, data_dir, "new", "c", tall)  # 已经切段认过
    older = _image(conn, data_dir, "old", "d", tall)  # 旧说明认的：全库重认（--redo）去认，那时已经切段
    short = _image(conn, data_dir, "new", "e", phone)  # 普通截图
    skipped = _image(conn, data_dir, "old", "f", tall)  # 记了跳过的
    never = _image(conn, data_dir, "old", "g", tall)  # 从没认过：不带 --redo 的识图会认
    _text(conn, whole, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, CHAT)
    _text(conn, unreadable, "deepseek-flash-vision:invalid", "", VISION_PROMPT_VERSION)
    _text(conn, tiled, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, TILED)
    _text(conn, older, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION - 1, CHAT)
    _text(conn, short, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, CHAT)
    _text(conn, skipped, "deepseek-flash-vision:skipped", "", VISION_PROMPT_VERSION)
    conn.commit()

    assert [row["content_hash"] for row in pending_tall_images(conn, data_dir, 10)] == [whole, unreadable]
    assert [row["content_hash"] for row in pending_tall_images(conn, data_dir, 1)] == [whole]
    assert tall_remaining(conn, data_dir) == {"images": 2, "tiles": 2 * tile_count(*tall)}
    # 全库重认照旧只挑旧说明认的和失败的，不管长不长。
    assert {row["content_hash"] for row in pending_images(conn, 10, redo=True)} == {older, never}

    seen: list[str] = []

    def recognize(path: Path) -> Recognition:
        seen.append(path.name)
        return Recognition("王小明：毛利率35%，比去年高", {**TILED, "blocks": []}, {"prompt": 10, "completion": 20})

    report = run_ocr(conn, data_dir, 1, recognize, engine="deepseek-flash-vision", redo=True, tall=True)
    assert report["images"] == 1 and seen == ["a.png"]
    row = conn.execute(
        "SELECT text, units, prompt_version FROM image_texts WHERE content_hash = %s", (whole,)
    ).fetchone()
    assert row["text"] == "王小明：毛利率35%，比去年高" and row["units"]["tiles"] == 2
    assert row["prompt_version"] == VISION_PROMPT_VERSION
    # 切段认过的不再挑，停了再开接着认剩下的。
    assert [found["content_hash"] for found in pending_tall_images(conn, data_dir, 10)] == [unreadable]
    assert tall_remaining(conn, data_dir) == {"images": 1, "tiles": tile_count(*tall)}


def test_failed_tall_redo_keeps_the_old_text(conn, settings):
    _articles(conn)
    data_dir = Path(settings.data_dir)
    whole = _image(conn, data_dir, "new", "a", (600, 2400))
    _text(conn, whole, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, CHAT)
    conn.commit()

    def broken(_path: Path) -> Recognition:
        raise RuntimeError("连不上")

    report = run_ocr(conn, data_dir, 5, broken, engine="deepseek-flash-vision", redo=True, tall=True)
    assert (report["images"], report["failed"], report["kept"]) == (1, 1, 1)
    row = conn.execute("SELECT engine, text, units FROM image_texts WHERE content_hash = %s", (whole,)).fetchone()
    assert row["engine"] == "deepseek-flash-vision" and row["text"] == "王小明：毛利率35%"
    assert "tiles" not in row["units"]
    assert [found["content_hash"] for found in pending_tall_images(conn, data_dir, 10)] == [whole]

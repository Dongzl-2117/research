"""research-ocr --tall：只挑以前整张认的长截图切段重认，认过的不再付一次钱，失败留着旧字。

长图切段以前，整张压到长边 1920 像素，1080×7237 的长截图送出去宽只剩 286，认出来的字错得多、改了意思。
11-10 晚以前切段认的几张没存每段结果（那时接缝会把重叠的一截写两遍），也由 --tall 重认一次。
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
TILED = {**CHAT, "tiles": 2}  # 11-10 晚以前切段认的：没存每段结果
JOINED = {**TILED, "parts": [CHAT, CHAT]}  # 现在切段认的：存着每段结果，接法改了能重接


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
    tiled = _image(conn, data_dir, "new", "c", tall)  # 已经切段认过，存着每段结果
    early = _image(conn, data_dir, "old", "h", tall)  # 11-10 晚以前切段认的，没存每段结果：要重认
    older = _image(conn, data_dir, "old", "d", tall)  # 旧说明认的：全库重认（--redo）去认，那时已经切段
    short = _image(conn, data_dir, "new", "e", phone)  # 普通截图
    skipped = _image(conn, data_dir, "old", "f", tall)  # 记了跳过的
    never = _image(conn, data_dir, "old", "g", tall)  # 从没认过：不带 --redo 的识图会认
    _text(conn, whole, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, CHAT)
    _text(conn, unreadable, "deepseek-flash-vision:invalid", "", VISION_PROMPT_VERSION)
    _text(conn, tiled, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, JOINED)
    _text(conn, early, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, TILED)
    _text(conn, older, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION - 1, CHAT)
    _text(conn, short, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, CHAT)
    _text(conn, skipped, "deepseek-flash-vision:skipped", "", VISION_PROMPT_VERSION)
    conn.commit()

    picked = [row["content_hash"] for row in pending_tall_images(conn, data_dir, 10)]
    assert picked[0] == whole and set(picked[1:]) == {unreadable, early} and len(picked) == 3
    assert [row["content_hash"] for row in pending_tall_images(conn, data_dir, 1)] == [whole]
    assert tall_remaining(conn, data_dir) == {"images": 3, "tiles": 3 * tile_count(*tall)}
    # 全库重认照旧只挑旧说明认的和失败的，不管长不长。
    assert {row["content_hash"] for row in pending_images(conn, 10, redo=True)} == {older, never}

    seen: list[str] = []

    def recognize(path: Path) -> Recognition:
        seen.append(path.name)
        return Recognition("王小明：毛利率35%，比去年高", {**JOINED, "blocks": []}, {"prompt": 10, "completion": 20})

    report = run_ocr(conn, data_dir, 1, recognize, engine="deepseek-flash-vision", redo=True, tall=True)
    assert report["images"] == 1 and seen == ["a.png"]
    # 结尾列出这次重认的长图挂在哪几篇上，拿去对原图核接缝。
    newest = conn.execute("SELECT id FROM articles WHERE external_id = 'new'").fetchone()["id"]
    assert report["touched"] == [newest]
    row = conn.execute(
        "SELECT text, units, prompt_version FROM image_texts WHERE content_hash = %s", (whole,)
    ).fetchone()
    assert row["text"] == "王小明：毛利率35%，比去年高" and row["units"]["tiles"] == 2
    assert row["prompt_version"] == VISION_PROMPT_VERSION
    # 切段认过的不再挑，停了再开接着认剩下的。
    assert {found["content_hash"] for found in pending_tall_images(conn, data_dir, 10)} == {unreadable, early}
    assert tall_remaining(conn, data_dir) == {"images": 2, "tiles": 2 * tile_count(*tall)}


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


def test_rejoin_rewrites_stored_tiles_and_the_article_without_calling_the_model(conn, settings):
    """接缝的接法改了：research-ocr --rejoin 按库里存的每段结果重接，图片文字、文章旁路和检索块跟着变，版本不动。"""
    from researchlib.ocr import rejoin_stored
    from researchlib.ocr_units import format_units, join_tiles

    _articles(conn)
    data_dir = Path(settings.data_dir)
    poster = "春季发布会\n软件与智能驾驶\n给每辆车一个助手\n6月15日 16:30"
    first = {
        "v": 1,
        "kind": "text",
        "blocks": [
            {"type": "text", "text": "下周一会有一场发布会，届时公布全新的系统"},
            {"type": "text", "text": poster},
        ],
    }
    second = {
        "v": 1,
        "kind": "text",
        "blocks": [
            {"type": "text", "text": "春季发布会\n软件与智能驾驶\n给每辆车一个助手"},
            {"type": "text", "text": "6月15日 16:30"},
            {"type": "text", "text": "到时候会讲清楚新系统怎么收费、哪些车型能升级"},
        ],
    }
    fresh = join_tiles([first, second])
    stale = {**fresh, "blocks": [*first["blocks"], *second["blocks"]]}  # 以前的接法：海报写了两遍
    tiled = _image(conn, data_dir, "new", "a", (600, 2400))
    before_parts = _image(conn, data_dir, "old", "b", (600, 2400))  # 11-10 以前切段认的，没存每段结果
    _text(conn, tiled, "deepseek-flash-vision", format_units(stale), VISION_PROMPT_VERSION, stale)
    _text(conn, before_parts, "deepseek-flash-vision", "王小明：毛利率35%", VISION_PROMPT_VERSION, TILED)
    conn.commit()

    assert rejoin_stored(conn, dry_run=True) == {"matched": 1, "changed": 1, "articles": 0, "dry_run": True}
    assert rejoin_stored(conn) == {"matched": 1, "changed": 1, "articles": 1, "dry_run": False}
    row = conn.execute(
        "SELECT engine, text, units, prompt_version FROM image_texts WHERE content_hash = %s", (tiled,)
    ).fetchone()
    assert row["units"] == fresh and row["text"] == format_units(fresh) and row["text"].count("春季发布会") == 1
    assert (row["engine"], row["prompt_version"]) == ("deepseek-flash-vision", VISION_PROMPT_VERSION)
    article = conn.execute("SELECT id, ocr_text FROM articles WHERE external_id = 'new'").fetchone()
    assert article["ocr_text"].count("春季发布会") == 1
    chunks = conn.execute(
        "SELECT body FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article["id"],)
    ).fetchall()
    assert chunks and "\n".join(chunk["body"] for chunk in chunks).count("春季发布会") == 1
    untouched = conn.execute("SELECT units FROM image_texts WHERE content_hash = %s", (before_parts,)).fetchone()
    assert untouched["units"] == TILED
    # 再跑一遍没有要改的
    assert rejoin_stored(conn) == {"matched": 1, "changed": 0, "articles": 0, "dry_run": False}

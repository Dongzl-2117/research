"""识图失败（:error）按次数退避、到上限改记 :invalid（审核 C3）。

以前记成 :error 的图每晚重认、没有上限；模型把思考额度用光交回空正文的，每晚都付一次钱。
"""

from pathlib import Path

from researchlib.llm import LLMError
from researchlib.ocr import OCR_ERROR_LIMIT, Recognition, pending_images, read_vision_reply, run_ocr
from researchlib.ocr_units import UnitsError

BODY = "茅台三季度利润还在涨，飞天批价跌了但渠道库存不高，系列酒拖累了收入。"


def _image(conn, settings, digit: str) -> str:
    conn.execute(
        """
        INSERT INTO articles (source_id, external_id, content_hash, title, published_at, body_md, body_text)
        VALUES ('fangwei-mp', %(ext)s, %(ext)s, '一篇', '2026-11-07 12:00+08', %(body)s, %(body)s)
        ON CONFLICT DO NOTHING
        """,
        {"ext": f"a-{digit}", "body": BODY},
    )
    content_hash = digit * 32
    conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash)
        SELECT id, source_id, %s, %s, %s FROM articles WHERE external_id = %s
        """,
        (f"{digit}.png", f"x/{digit}.png", content_hash, f"a-{digit}"),
    )
    path = Path(settings.data_dir) / "images" / "x" / f"{digit}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + digit.encode())
    conn.commit()
    return content_hash


def _row(conn, content_hash: str):
    conn.commit()
    return conn.execute(
        "SELECT engine, failures, failed_at FROM image_texts WHERE content_hash = %s", (content_hash,)
    ).fetchone()


def _pending(conn) -> set[str]:
    conn.commit()
    return {row["content_hash"] for row in pending_images(conn, 50)}


def _age(conn, content_hash: str, interval: str) -> None:
    conn.execute(
        f"UPDATE image_texts SET failed_at = now() - interval '{interval}' WHERE content_hash = %s", (content_hash,)
    )
    conn.commit()


def test_failed_image_backs_off_and_gives_up_after_the_limit(conn, settings):
    bad = _image(conn, settings, "a")

    def broken(_path):
        raise LLMError("DeepSeek 接口返回 400：image format not supported")

    run_ocr(conn, Path(settings.data_dir), 10, broken, engine="vision")
    row = _row(conn, bad)
    assert row["engine"] == "vision:error" and row["failures"] == 1 and row["failed_at"] is not None
    # 刚失败的不马上再认；过了 20 小时（下一晚）再认。
    assert bad not in _pending(conn)
    _age(conn, bad, "21 hours")
    assert bad in _pending(conn)
    run_ocr(conn, Path(settings.data_dir), 10, broken, engine="vision")
    assert _row(conn, bad)["failures"] == 2
    # 失败 2 次隔 3 天。
    _age(conn, bad, "2 days")
    assert bad not in _pending(conn)
    _age(conn, bad, "4 days")
    report = run_ocr(conn, Path(settings.data_dir), 10, broken, engine="vision")
    row = _row(conn, bad)
    assert OCR_ERROR_LIMIT == 3 and row["engine"] == "vision:invalid" and row["failures"] == 3
    assert report["invalid"] == 1 and report["failed"] == 0
    _age(conn, bad, "30 days")
    assert bad not in _pending(conn)


def test_rate_limits_do_not_count_and_success_clears_the_count(conn, settings):
    image = _image(conn, settings, "b")

    def limited(_path):
        raise LLMError("DeepSeek 接口返回 429：rate limited")

    run_ocr(conn, Path(settings.data_dir), 10, limited, engine="vision")
    row = _row(conn, image)
    # 限流和这张图无关：不算次数，下一次照常认。
    assert row["engine"] == "vision:error" and row["failures"] == 0 and row["failed_at"] is None
    assert image in _pending(conn)
    run_ocr(conn, Path(settings.data_dir), 10, lambda _p: Recognition("毛利率：35%"), engine="vision")
    row = _row(conn, image)
    assert row["engine"] == "vision" and row["failures"] == 0 and row["failed_at"] is None


def test_empty_reply_is_invalid_right_away(conn, settings):
    image = _image(conn, settings, "c")

    def empty(_path):
        raise LLMError("DeepSeek 接口返回的正文为空")

    report = run_ocr(conn, Path(settings.data_dir), 10, empty, engine="vision")
    assert _row(conn, image)["engine"] == "vision:invalid" and report["invalid"] == 1
    assert image not in _pending(conn)


def test_deeply_nested_reply_counts_as_unreadable_structure():
    import pytest

    deep = '{"kind": "chat", "blocks": ' + "[" * 100000 + "]" * 100000 + "}"
    with pytest.raises(UnitsError):
        read_vision_reply(deep)

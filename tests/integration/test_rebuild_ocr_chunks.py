"""整理规则改了以后，research-rebuild-chunks --ocr 按新规则重切检索里的图片文字块，不动 articles.ocr_text。"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from researchlib.chunks import OCR_HEADING, OCR_ORD_BASE, rebuild_ocr_chunks
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.render import image_anchor
from researchlib.sources import load_sources

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 10-28 以前切的块里还留着这些：公众号末尾的星球优惠券卡、群聊截图里的邀请提示和群友昵称。
COUPON = "方伟看十年\n扫码领取优惠券\n仅限前1000名\n2025/05/05 00:01至2025/12/01 12:00\n66元立减金\n知识星球\n"
CHAT = (
    "微信\n看十年交流群(120)\n王小明\n"
    '大家觉得组织是护城河么你邀请"李四"加入了群聊撤销 "李四"与群里其他人都不是朋友关系，请注意隐私安全\n'
    "方伟Rey\n组织是支撑，不是护城河本身，业务上的护城河就那几类\n"
)


def _png(path: Path, marker: bytes) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker)


def _ocr_bodies(conn, article_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT body FROM article_chunks WHERE article_id = %s AND lang = 'ocr' ORDER BY ord",
        (article_id,),
    ).fetchall()
    return [row["body"] for row in rows]


def test_rebuild_applies_current_rules_to_old_ocr_chunks(conn, settings, tmp_path):
    coupon = tmp_path / "coupon.png"
    chat = tmp_path / "chat.png"
    _png(coupon, b"\x02" * 24)
    _png(chat, b"\x03" * 24)
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-mp")
    doc = ParsedDoc(
        external_id="ocr-rebuild-1",
        title="都是卖服装，为什么有的有护城河",
        published_at=datetime(2025, 5, 6, 7, 0, tzinfo=SHANGHAI),
        body_md="护城河要看业务本身。\n\n![](chat.png)\n\n![](coupon.png)\n",
        raw_meta={},
        image_refs=[(coupon.name, coupon), (chat.name, chat)],
    )
    store_documents(
        conn, source=source, docs=[doc], data_dir=settings.data_dir, archive_files=1, images_in_archive=2, errors=[]
    )
    article_id = conn.execute("SELECT id FROM articles WHERE external_id = 'ocr-rebuild-1'").fetchone()["id"]
    hashes = {}
    for ref, text in (("coupon.png", COUPON), ("chat.png", CHAT)):
        digest = conn.execute(
            "SELECT content_hash FROM article_images WHERE article_id = %s AND original_ref = %s",
            (article_id, ref),
        ).fetchone()["content_hash"]
        conn.execute(
            "INSERT INTO image_texts (content_hash, engine, text) VALUES (%s, 'test', %s)"
            " ON CONFLICT (content_hash) DO UPDATE SET text = EXCLUDED.text",
            (digest, text),
        )
        hashes[ref] = digest
    # 旧规则切出来的块：原样带着优惠券卡和邀请提示。旁路也是旧的。
    conn.execute("DELETE FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article_id,))
    for index, ref in enumerate(("chat.png", "coupon.png")):
        conn.execute(
            "INSERT INTO article_chunks (article_id, ord, heading, body, lang, anchor)"
            " VALUES (%s, %s, %s, %s, 'ocr', %s)",
            (
                article_id,
                OCR_ORD_BASE + index,
                OCR_HEADING,
                CHAT if ref == "chat.png" else COUPON,
                image_anchor(hashes[ref]),
            ),
        )
    conn.execute("UPDATE articles SET ocr_text = %s, ocr_hash = 'old' WHERE id = %s", (COUPON + CHAT, article_id))
    conn.commit()

    # 先数一遍：只数不写。
    scanned, changed = rebuild_ocr_chunks(conn, dry_run=True)
    assert scanned >= 1 and changed >= 1
    assert any("优惠券" in body for body in _ocr_bodies(conn, article_id))

    assert rebuild_ocr_chunks(conn)[1] == changed

    bodies = _ocr_bodies(conn, article_id)
    joined = "\n".join(bodies)
    for leaked in ("优惠券", "立减金", "李四", "加入了群聊", "隐私安全"):
        assert leaked not in joined, leaked
    assert "大家觉得组织是护城河么" in joined
    assert "组织是支撑，不是护城河本身" in joined
    anchors = {
        row["anchor"]
        for row in conn.execute(
            "SELECT anchor FROM article_chunks WHERE article_id = %s AND lang = 'ocr'", (article_id,)
        ).fetchall()
    }
    assert anchors == {image_anchor(hashes["chat.png"])}
    # 旁路不动：改了 ocr_hash，正文短的帖子会被排去重做摘要和观点。
    kept = conn.execute("SELECT ocr_text, ocr_hash FROM articles WHERE id = %s", (article_id,)).fetchone()
    assert kept["ocr_hash"] == "old"
    assert kept["ocr_text"] == COUPON + CHAT
    # 再跑一遍块没变，不动。
    assert rebuild_ocr_chunks(conn)[1] == 0
    assert _ocr_bodies(conn, article_id) == bodies

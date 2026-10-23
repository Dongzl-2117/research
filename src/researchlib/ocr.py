"""图片里的字进检索（OCR）。

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。这里把本地图片（归档里带的、
从外链下载来的）交给 RapidOCR 识别：按图片内容哈希只识别一次，结果存进 image_texts；识别出的字够多的图，
切成段落块进检索（article_chunks.lang = 'ocr'，标题「图片里的字」，锚点指向文章页上那张图）。
公司提及不看图片里的字，免得水印、表情包里的字乱记。

RapidOCR（onnxruntime + OpenCV）比较大，不装进主镜像，另有一个 ocr 镜像：

    docker compose --profile ocr build ocr
    docker compose --profile ocr run --rm ocr research-ocr --limit 500

夜间任务设 NIGHTLY_OCR=1 时每晚补一批。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from researchlib.chunks import replace_ocr_chunks
from researchlib.eligibility import (
    IDENTITY_KEY,
    is_zsxq,
    model_cache_safe_article,
    model_cache_safe_sql,
    project_article,
)
from researchlib.ocr_bubbles import bubble_color
from researchlib.wechat_dialogue import (
    BUBBLE_GUEST,
    BUBBLE_SELF,
    BUBBLE_UNKNOWN,
    present_ocr_pieces,
    present_stored_ocr,
)

logger = logging.getLogger("researchlib.ocr")

ENGINE = "rapidocr"
# 置信度低于这个的文字框不要：多半是把花纹、二维码认成了字。
MIN_SCORE = 0.5
OCR_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
# 检索块要 20 个字；交给观点和摘要的旁路、以及阅读页放宽，短截图也能进正文。
READING_MIN_CHARS = 8
# 正文短于这个字数时，图片文字和上次抽摘要、观点时不一致就要重抽。长文不因为后补一张图整篇重来。
SHORT_BODY_CHARS = 400
_MEANINGFUL = re.compile(r"[0-9A-Za-z一-鿿]")


class OcrUnavailable(Exception):
    pass


def ocr_allowed_sql(alias: str = "a") -> str:
    """Images on a guest topic have no verified ownership, even with an owner reply."""
    prefix = f"{alias}." if alias else ""
    return (
        f"({model_cache_safe_sql(alias)} AND "
        f"({prefix}raw_meta->'{IDENTITY_KEY}'->>'kind') IS DISTINCT FROM 'owner_reply_context')"
    )


def _ocr_allowed_article(article) -> bool:
    if article is None or not model_cache_safe_article(article):
        return False
    projected = project_article(article)
    return projected is not None and not projected.get("owner_reply_context")


def _ocr_article(conn, article_id: int):
    return conn.execute(
        """
        SELECT a.id, a.source_id, a.raw_meta, a.body_md, a.ocr_text, a.ocr_hash,
               s.kind AS source_kind
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE a.id = %s
        """,
        (article_id,),
    ).fetchone()


def _image_ref_allowed(article, original_ref: str) -> bool:
    if not is_zsxq(article):
        return True
    # A reimport can leave old comment attachments in article_images. Their
    # presence on the same owner row does not prove ownership of each image.
    from researchlib.ingest.parse import image_refs_in
    from researchlib.render import image_destination

    projected = project_article(article)
    if projected is None or projected.get("owner_reply_context"):
        return False
    allowed = {image_destination(ref) for ref in image_refs_in(projected.get("body_md") or "")}
    return bool(original_ref) and image_destination(original_ref) in allowed


def _image_articles(conn, digest: str, article_ids: list[int] | None = None) -> list[dict]:
    where = ["i.content_hash = %s", "a.duplicate_of IS NULL", ocr_allowed_sql("a")]
    params: list = [digest]
    if article_ids:
        where.append("a.id = ANY(%s)")
        params.append(list(article_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT a.id AS article_id, a.source_id, a.raw_meta, a.body_md, i.original_ref,
               s.kind AS source_kind
        FROM article_images i
        JOIN articles a ON a.id = i.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE {" AND ".join(where)}
        """,
        tuple(params),
    ).fetchall()
    # Recheck in Python as well: a stale selected candidate or a shared image
    # hash must not make the executor process an excluded guest article.
    allowed: dict[int, dict] = {}
    for row in rows:
        if _ocr_allowed_article(row) and _image_ref_allowed(row, row.get("original_ref") or ""):
            allowed[row["article_id"]] = row
    return list(allowed.values())


def order_text(items, *, rgb_image=None) -> str:
    """RapidOCR 的结果 [[框, 文字, 置信度], …] 按阅读顺序拼起来：从上到下分行，同一行从左到右。

    有原图时按气泡底色写入明确的绿/灰标记，不用左右位置猜颜色。无法确认的行标记为未知。
    没有原图的调用只排序，不生成说话人证据。旧侧标记仍由呈现层兼容读取。
    """
    boxes: list[tuple[float, float, float, float, str]] = []
    for item in items or []:
        if not isinstance(item, list | tuple) or len(item) < 2:
            continue
        box, text = item[0], str(item[1] or "").strip()
        score = float(item[2]) if len(item) > 2 and item[2] is not None else 1.0
        if not text or score < MIN_SCORE:
            continue
        try:
            ys = [float(point[1]) for point in box]
            xs = [float(point[0]) for point in box]
        except (TypeError, IndexError, ValueError):
            continue
        right = max(xs)
        boxes.append((min(ys), max(ys), min(xs), right, text))
    boxes.sort(key=lambda box: (box[0], box[2]))
    lines: list[dict] = []
    for top, bottom, left, right, text in boxes:
        color = bubble_color(rgb_image, (left, top, right, bottom))
        marker = {"green": BUBBLE_SELF, "gray": BUBBLE_GUEST}.get(color, BUBBLE_UNKNOWN)
        if rgb_image is None:
            marker = ""
        height = max(1.0, bottom - top)
        center = (top + bottom) / 2
        if lines and abs(center - lines[-1]["center"]) <= 0.5 * max(height, lines[-1]["height"]):
            lines[-1]["items"].append((left, text, marker))
        else:
            lines.append({"center": center, "height": height, "items": [(left, text, marker)]})
    rendered: list[str] = []
    for line in lines:
        items_on_line = sorted(line["items"])
        # Two differently colored bubbles at the same y are separate turns, not
        # one joined line whose first box assigns the whole line's speaker.
        current_marker = None
        pieces: list[str] = []
        for _left, piece, marker in items_on_line:
            if pieces and marker != current_marker:
                rendered.append((current_marker or "") + "  ".join(pieces))
                pieces = []
            current_marker = marker
            pieces.append(piece)
        if pieces:
            rendered.append((current_marker or "") + "  ".join(pieces))
    return "\n".join(rendered)


def rapid_engine() -> Callable[[Path], str]:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise OcrUnavailable(
            "这个镜像里没装 OCR。用 ocr 镜像跑：docker compose --profile ocr run --rm ocr research-ocr"
        ) from exc
    engine = RapidOCR()

    def recognize(path: Path) -> str:
        import cv2

        result, _elapsed = engine(str(path))
        image = cv2.imread(str(path))
        if image is None:
            raise OcrUnavailable(f"无法读取 OCR 原图颜色：{path.name}")
        return order_text(result, rgb_image=cv2.cvtColor(image, cv2.COLOR_BGR2RGB))

    return recognize


def pending_images(conn, limit: int, *, redo: bool = False, article_ids: list[int] | None = None):
    """还没识别过的本地图片，按引用它的文章从新到旧。重复文章里的图不算。

    redo 时连 image_texts 里已有的哈希也再认一遍。article_ids 只认这几篇挂着的图。
    """
    where = ["a.duplicate_of IS NULL", ocr_allowed_sql("a")]
    params: list = []
    if article_ids:
        where.append("i.article_id = ANY(%s)")
        params.append(list(article_ids))
    if not redo:
        where.append("NOT EXISTS (SELECT 1 FROM image_texts t WHERE t.content_hash = i.content_hash)")
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT i.content_hash, min(i.storage_path) AS storage_path
        FROM article_images i
        JOIN articles a ON a.id = i.article_id
        WHERE {" AND ".join(where)}
        GROUP BY i.content_hash
        ORDER BY max(a.published_at) DESC NULLS LAST
        LIMIT %s
        """,
        tuple(params),
    ).fetchall()
    # Apply the exact projected-image relation before returning candidates;
    # SQL row eligibility alone cannot validate leftover attachment ownership.
    return [row for row in rows if _image_articles(conn, row["content_hash"], article_ids)]


def useful_image_text(text: str) -> bool:
    """汉字、字母、数字够 READING_MIN_CHARS 个，才算图里有可读的字。

    空白、水印、几个字的装饰不算。刚过线的拿不准，算有用，不能漏。
    不按「是不是微信、是不是表格」分：有可读的字就留。
    """
    return len(_MEANINGFUL.findall(text or "")) >= READING_MIN_CHARS


def compose_ocr_text(pieces: list[str]) -> str:
    """按图片顺序拼成观点和摘要能读的旁路正文。

    太短的（水印、表情）丢掉。微信群聊截图整理成「方伟：」「网友：」，
    表格和长文保持原文。阅读页用同一套规则，见 wechat_dialogue。
    """
    useful: list[str] = []
    for piece in pieces:
        text = (piece or "").strip()
        if useful_image_text(text):
            useful.append(text)
    if not useful:
        return ""
    return present_ocr_pieces(useful)


def hash_ocr(text: str) -> str:
    if not (text or "").strip():
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def append_ocr(body: str, ocr_text: str) -> str:
    """正文后面接上图片里的字。没有识别结果时原样返回。"""
    extra = (ocr_text or "").strip()
    if not extra:
        return body or ""
    base = (body or "").rstrip()
    block = "【图片里的字】\n" + extra
    return f"{base}\n\n{block}" if base else block


def refresh_article_ocr(conn, article_id: int) -> str:
    """按这篇已识别的图重写 articles.ocr_text / ocr_hash。返回哈希。"""
    article = _ocr_article(conn, article_id)
    if not _ocr_allowed_article(article):
        return ""
    rows = conn.execute(
        """
        SELECT t.text, i.original_ref
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        WHERE i.article_id = %s
        ORDER BY i.id
        """,
        (article_id,),
    ).fetchall()
    text = compose_ocr_text([
        row["text"] for row in rows if _image_ref_allowed(article, row.get("original_ref") or "")
    ])
    digest = hash_ocr(text)
    current = conn.execute(
        "SELECT ocr_text, ocr_hash FROM articles WHERE id = %s",
        (article_id,),
    ).fetchone()
    if current is not None and (current["ocr_text"] or "") == text and (current["ocr_hash"] or "") == digest:
        return digest
    conn.execute(
        "UPDATE articles SET ocr_text = %s, ocr_hash = %s WHERE id = %s",
        (text, digest, article_id),
    )
    return digest


def article_ocr(conn, article_id: int) -> tuple[str, str]:
    """摘要和观点读的图片文字。

    这篇还挂着 image_texts 时，先按当前规则从原始识别重写 articles.ocr_text。
    阅读页本来就现解析 image_texts；观点如果只重跑库里已经写成「方伟：」的旁路，
    账号栏已经被拿掉，引用回复分不回来。没有图时，仍把库里的旁路再整理一次。
    """
    article = _ocr_article(conn, article_id)
    if not _ocr_allowed_article(article):
        return "", ""
    has_images = conn.execute(
        """
        SELECT 1
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        WHERE i.article_id = %s
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    if has_images or is_zsxq(article):
        refresh_article_ocr(conn, article_id)
    row = conn.execute(
        "SELECT ocr_text, ocr_hash FROM articles WHERE id = %s",
        (article_id,),
    ).fetchone()
    if row is None:
        return "", ""
    cleaned = present_stored_ocr(row["ocr_text"] or "")
    digest = hash_ocr(cleaned)
    if cleaned != (row["ocr_text"] or "") or digest != (row["ocr_hash"] or ""):
        conn.execute(
            "UPDATE articles SET ocr_text = %s, ocr_hash = %s WHERE id = %s",
            (cleaned, digest, article_id),
        )
    return cleaned, digest


def run_ocr(
    conn,
    data_dir: Path,
    limit: int,
    recognize: Callable[[Path], str],
    engine: str = ENGINE,
    deadline: float | None = None,
    *,
    redo: bool = False,
    article_ids: list[int] | None = None,
) -> dict:
    """识别一批图片，重切引用它们的文章的图片文字块。返回计数。"""
    report = {"images": 0, "with_text": 0, "failed": 0, "missing": 0, "articles": 0}
    touched: set[int] = set()
    for row in pending_images(conn, limit, redo=redo, article_ids=article_ids):
        if deadline is not None and time.monotonic() > deadline:
            break
        articles = _image_articles(conn, row["content_hash"], article_ids)
        if not articles:
            continue
        image_root = (Path(data_dir) / "images").resolve()
        path = (image_root / row["storage_path"]).resolve()
        if not path.is_relative_to(image_root) or not path.is_file():
            report["missing"] += 1
            continue
        used = engine
        if path.suffix.lower() not in OCR_EXTS:
            text = ""
            used = f"{engine}:skipped"
        else:
            try:
                text = recognize(path)
            except Exception as exc:  # 单张图坏了不拖垮整批
                logger.warning("图片识别失败 %s：%s", path, exc)
                text = ""
                used = f"{engine}:error"
                report["failed"] += 1
        conn.execute(
            """
            INSERT INTO image_texts (content_hash, engine, text) VALUES (%s, %s, %s)
            ON CONFLICT (content_hash) DO UPDATE SET engine = EXCLUDED.engine, text = EXCLUDED.text, created_at = now()
            """,
            (row["content_hash"], used, text or ""),
        )
        report["images"] += 1
        if text.strip():
            report["with_text"] += 1
        for article in articles:
            touched.add(article["article_id"])
            if text.strip() and replace_ocr_chunks(conn, article["article_id"]):
                report["articles"] += 1
        conn.commit()
    for article_id in sorted(touched):
        refresh_article_ocr(conn, article_id)
    if touched:
        conn.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了，2 这个镜像里没装 OCR。"""
    parser = argparse.ArgumentParser(description="识别图片里的字，切成段落块进检索")
    parser.add_argument("--limit", type=int, default=500, help="这次最多识别多少张图")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停，0 表示不限")
    parser.add_argument("--redo", action="store_true", help="连已经识别过的图也再认一遍（重跑存量）")
    parser.add_argument("--article", action="append", type=int, default=[], help="只认这篇文章里的图，可重复")
    args = parser.parse_args(argv)
    try:
        recognize = rapid_engine()
    except OcrUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    conn = prepare(settings)
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes > 0 else None
    try:
        report = run_ocr(
            conn,
            settings.data_dir,
            args.limit,
            recognize,
            deadline=deadline,
            redo=args.redo,
            article_ids=args.article or None,
        )
    finally:
        conn.close()
    print(
        f"识别 {report['images']} 张图，有字的 {report['with_text']} 张，失败 {report['failed']} 张，"
        f"文件缺失 {report['missing']} 张，更新了 {report['articles']} 篇的图片文字"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

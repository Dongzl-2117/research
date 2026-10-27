"""图片里的字进检索（OCR）。

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。这里把本地图片（归档里带的、
从外链下载来的）交给 RapidOCR 识别：按图片内容哈希只识别一次，结果存进 image_texts；识别出的字够多的图，
切成段落块进检索（article_chunks.lang = 'ocr'，标题「图片里的字」，锚点指向文章页上那张图）。
公司提及默认不看图片里的字，免得水印、表情包乱挂；正文短于 SHORT_BODY_CHARS 时例外，
截图帖要挂上图里的公司才能抽分家观点。

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
from researchlib.wechat_dialogue import SIDE_SELF, present_ocr_pieces, present_stored_ocr

logger = logging.getLogger("researchlib.ocr")

ENGINE = "rapidocr"
# 置信度低于这个的文字框不要：多半是把花纹、二维码认成了字。
MIN_SCORE = 0.5
# 行的左边缘超过画面宽度的这个比例，当作靠右的绿色气泡（作者自己）。左侧昵称和灰气泡在这之下。
_SELF_SIDE = 0.40
OCR_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
# 检索块要 20 个字；交给观点和摘要的旁路、以及阅读页放宽，短截图也能进正文。
READING_MIN_CHARS = 8
# 正文短于这个字数时，图片文字和上次抽摘要、观点时不一致就要重抽。长文不因为后补一张图整篇重来。
SHORT_BODY_CHARS = 400
_MEANINGFUL = re.compile(r"[0-9A-Za-z一-鿿]")


class OcrUnavailable(Exception):
    pass


def order_text(items) -> str:
    """RapidOCR 的结果 [[框, 文字, 置信度], …] 按阅读顺序拼起来：从上到下分行，同一行从左到右。

    靠右的行（绿色气泡，作者自己）前面加一个不显示的侧标记，对话整理时认成方伟。
    左侧的灰气泡不加。旧的、只有文字没有坐标的识别结果没有这个标记。
    """
    boxes: list[tuple[float, float, float, float, str]] = []
    width = 0.0
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
        width = max(width, right)
        boxes.append((min(ys), max(ys), min(xs), right, text))
    boxes.sort(key=lambda box: (box[0], box[2]))
    lines: list[dict] = []
    for top, bottom, left, _right, text in boxes:
        height = max(1.0, bottom - top)
        center = (top + bottom) / 2
        if lines and abs(center - lines[-1]["center"]) <= 0.5 * max(height, lines[-1]["height"]):
            lines[-1]["items"].append((left, text))
        else:
            lines.append({"center": center, "height": height, "items": [(left, text)]})
    rendered: list[str] = []
    for line in lines:
        items_on_line = sorted(line["items"])
        text = "  ".join(piece for _left, piece in items_on_line)
        if width and items_on_line[0][0] >= _SELF_SIDE * width:
            text = SIDE_SELF + text
        rendered.append(text)
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
        result, _elapsed = engine(str(path))
        return order_text(result)

    return recognize


def pending_images(conn, limit: int, *, redo: bool = False, article_ids: list[int] | None = None):
    """还没识别过的本地图片，按引用它的文章从新到旧。重复文章里的图不算。

    redo 时连 image_texts 里已有的哈希也再认一遍。article_ids 只认这几篇挂着的图。
    """
    where = ["a.duplicate_of IS NULL"]
    params: list = []
    if article_ids:
        where.append("i.article_id = ANY(%s)")
        params.append(list(article_ids))
    if not redo:
        where.append("NOT EXISTS (SELECT 1 FROM image_texts t WHERE t.content_hash = i.content_hash)")
    params.append(limit)
    return conn.execute(
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
    rows = conn.execute(
        """
        SELECT t.text
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        WHERE i.article_id = %s
        ORDER BY i.id
        """,
        (article_id,),
    ).fetchall()
    text = compose_ocr_text([row["text"] for row in rows])
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
    # 短正文截图帖：OCR 变了就要重挂提及，否则观点 gap 只跟旧挂载走。
    row = conn.execute(
        "SELECT length(coalesce(body_text, '')) AS n FROM articles WHERE id = %s",
        (article_id,),
    ).fetchone()
    if row is not None and int(row["n"] or 0) < SHORT_BODY_CHARS:
        from researchlib.mentions import rebuild_article

        rebuild_article(conn, int(article_id))
    return digest


def article_ocr(conn, article_id: int) -> tuple[str, str]:
    """摘要和观点读的图片文字。

    这篇还挂着 image_texts 时，先按当前规则从原始识别重写 articles.ocr_text。
    阅读页本来就现解析 image_texts；观点如果只重跑库里已经写成「方伟：」的旁路，
    账号栏已经被拿掉，引用回复分不回来。没有图时，仍把库里的旁路再整理一次。
    """
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
    if has_images:
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
        path = Path(data_dir) / "images" / row["storage_path"]
        if not path.is_file():
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
        for article in conn.execute(
            "SELECT DISTINCT article_id FROM article_images WHERE content_hash = %s", (row["content_hash"],)
        ).fetchall():
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

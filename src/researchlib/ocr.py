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
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from researchlib.chunks import replace_ocr_chunks

logger = logging.getLogger("researchlib.ocr")

ENGINE = "rapidocr"
# 置信度低于这个的文字框不要：多半是把花纹、二维码认成了字。
MIN_SCORE = 0.5
OCR_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})


class OcrUnavailable(Exception):
    pass


def order_text(items) -> str:
    """RapidOCR 的结果 [[框, 文字, 置信度], …] 按阅读顺序拼起来：从上到下分行，同一行从左到右。"""
    boxes: list[tuple[float, float, float, str]] = []
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
        boxes.append((min(ys), max(ys), min(xs), text))
    boxes.sort(key=lambda box: (box[0], box[2]))
    lines: list[dict] = []
    for top, bottom, left, text in boxes:
        height = max(1.0, bottom - top)
        center = (top + bottom) / 2
        if lines and abs(center - lines[-1]["center"]) <= 0.5 * max(height, lines[-1]["height"]):
            lines[-1]["items"].append((left, text))
        else:
            lines.append({"center": center, "height": height, "items": [(left, text)]})
    return "\n".join("  ".join(text for _left, text in sorted(line["items"])) for line in lines)


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


def pending_images(conn, limit: int):
    """还没识别过的本地图片，按引用它的文章从新到旧。重复文章里的图不算。"""
    return conn.execute(
        """
        SELECT i.content_hash, min(i.storage_path) AS storage_path
        FROM article_images i
        JOIN articles a ON a.id = i.article_id
        WHERE a.duplicate_of IS NULL
          AND NOT EXISTS (SELECT 1 FROM image_texts t WHERE t.content_hash = i.content_hash)
        GROUP BY i.content_hash
        ORDER BY max(a.published_at) DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


def run_ocr(
    conn,
    data_dir: Path,
    limit: int,
    recognize: Callable[[Path], str],
    engine: str = ENGINE,
    deadline: float | None = None,
) -> dict:
    """识别一批图片，重切引用它们的文章的图片文字块。返回计数。"""
    report = {"images": 0, "with_text": 0, "failed": 0, "missing": 0, "articles": 0}
    for row in pending_images(conn, limit):
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
                if replace_ocr_chunks(conn, article["article_id"]):
                    report["articles"] += 1
        conn.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了，2 这个镜像里没装 OCR。"""
    parser = argparse.ArgumentParser(description="识别图片里的字，切成段落块进检索")
    parser.add_argument("--limit", type=int, default=500, help="这次最多识别多少张图")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停，0 表示不限")
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
        report = run_ocr(conn, settings.data_dir, args.limit, recognize, deadline=deadline)
    finally:
        conn.close()
    print(
        f"识别 {report['images']} 张图，有字的 {report['with_text']} 张，失败 {report['failed']} 张，"
        f"文件缺失 {report['missing']} 张，更新了 {report['articles']} 篇的图片文字"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

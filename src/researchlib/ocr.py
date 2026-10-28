"""图片里的字进检索（OCR / 识图）。

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。默认把本地图片交给
站内 deepseek-flash 识图（``llm.chat``，多模态 content），按内容哈希只认一次，结果存进
image_texts（engine 如 ``deepseek-flash-vision``）。识别出的字够多的图，切成段落块进检索
（article_chunks.lang = 'ocr'，标题「图片里的字」，锚点指向文章页上那张图）。
公司提及默认不看图片里的字，免得水印、表情包乱挂；正文短于 SHORT_BODY_CHARS 时例外，
截图帖要挂上图里的公司才能抽分家观点。

主镜像 / app 容器就能跑识图，不必装 onnx。RapidOCR 只作显式回退（``--engine rapidocr``
或 ocr 镜像），单张识图失败不悄悄改走 RapidOCR：记 ``engine`` 以 ``:error`` 结尾、正文为空，
下次不带 ``--redo`` 也会重试，避免静默脏字。

    docker compose exec -T app research-ocr --limit 50
    docker compose --profile ocr run --rm ocr research-ocr --engine rapidocr --limit 500

夜间任务设 NIGHTLY_OCR=1 时，在 app 容器里走识图补一批。站内触发见 POST /api/v1/ocr。
"""

from __future__ import annotations

import argparse
import base64
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
# CLI / API 的选择，不是写入 image_texts.engine 的名字。
ENGINE_CHOICES = ("vision", "rapidocr", "auto")
DEFAULT_ENGINE_CHOICE = "vision"
# 置信度低于这个的文字框不要：多半是把花纹、二维码认成了字。
MIN_SCORE = 0.5
# 行的左边缘超过画面宽度的这个比例，当作靠右的绿色气泡（作者自己）。左侧昵称和灰气泡在这之下。
_SELF_SIDE = 0.40
OCR_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
# 识图请求体太大时跳过，不反复打接口。手机截图远小于这个。
MAX_VISION_BYTES = 10 * 1024 * 1024
# 经 mini 上 DeepSeek en1 转发时，过大的 data-url 会在 TLS 中途被掐断。
# 压到这个字节数以下再 base64，payload 大约 150KB 内，和问库分批上限同一量级。
MAX_VISION_SEND_BYTES = 110_000
VISION_MAX_SIDE = 1280
VISION_JPEG_QUALITY = 70
VISION_TRANSIENT_TRIES = 3
VISION_MAX_TOKENS = 8192  # 转写不需要站点默认 393216，否则流式思考撑爆容器
# 检索块要 20 个字；交给观点和摘要的旁路、以及阅读页放宽，短截图也能进正文。
READING_MIN_CHARS = 8
# 正文短于这个字数时，图片文字和上次抽摘要、观点时不一致就要重抽。长文不因为后补一张图整篇重来。
SHORT_BODY_CHARS = 400
_MEANINGFUL = re.compile(r"[0-9A-Za-z一-鿿]")
_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}

# 写进代码，单测对短语。模型输出仍会再经 wechat_dialogue 剥界面字。
VISION_PROMPT = """\
你在把投资研究截图转成可检索的纯文字。只输出图里的字，不要额外评论，不要解释，不要用 markdown。

若是微信或知识星球聊天截图：
- 按气泡从上到下逐条转写，一条气泡一行。
- 绿色气泡（自己发的）写成「方伟：」。
- 其他人一律写成「网友：」，禁止输出真实昵称（不要写 Aaron、Henri、Kevin 或任何群昵称）。
- 剥掉界面字，不要输出：可按住转文字、回到引用位置、设为群待办、
  继续提问、AI搜索、状态栏时间、群名、雪球、LIVE、加人会员群、小程序卡片、输入框。
- 看不清的字写 ?，禁止编造。

若是表格或柱状图：
- 公司与数字一一对应，保留行列（公司、指标、数字写在同一行）。
- 看不清写 ?，禁止编造数字或公司名。

其它截图按阅读顺序逐行转写。看不清写 ?，禁止编造。不要额外评论。
"""


class OcrUnavailable(Exception):
    pass


class OcrImageSkipped(Exception):
    """这张图不再试（太大）。run_ocr 记 :skipped，不占重试。"""


def _bgr_is_wechat_green(bgr) -> bool:
    """微信自己发的绿气泡约 RGB(149, 236, 105)。抽样点偏绿就当本人。"""
    try:
        blue, green, red = (int(v) for v in bgr[:3])
    except (TypeError, ValueError):
        return False
    return green > 150 and green > red + 20 and green > blue + 20 and red < 210


def _line_is_self_bubble(
    image_bgr,
    left: float,
    right: float,
    top: float,
    bottom: float,
    width: float,
) -> bool:
    """先看气泡颜色；没有图时退回「行左缘过中线偏右」的旧规则。"""
    if image_bgr is not None:
        try:
            height_img, width_img = image_bgr.shape[:2]
        except (AttributeError, TypeError, ValueError):
            height_img = width_img = 0
        if height_img and width_img:
            cy = int(max(0, min(height_img - 1, (top + bottom) / 2)))
            # 文字左缘内侧几像素是气泡底色；绿=本人，白/灰=网友。
            for dx in (-8, -16, -24, 8):
                x = int(left + dx)
                if 0 <= x < width_img and _bgr_is_wechat_green(image_bgr[cy, x]):
                    return True
            return False
    return bool(width) and left >= _SELF_SIDE * width


def order_text(items, image_bgr=None) -> str:
    """RapidOCR 的结果 [[框, 文字, 置信度], …] 按阅读顺序拼起来：从上到下分行，同一行从左到右。

    绿色气泡（作者自己）前面加一个不显示的侧标记，对话整理时认成方伟。
    有原图时按气泡颜色认；没有图时退回「行左缘靠右」的旧规则。左侧灰气泡不加。
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
    for top, bottom, left, right, text in boxes:
        height = max(1.0, bottom - top)
        center = (top + bottom) / 2
        if lines and abs(center - lines[-1]["center"]) <= 0.5 * max(height, lines[-1]["height"]):
            lines[-1]["items"].append((left, right, text))
            lines[-1]["top"] = min(lines[-1]["top"], top)
            lines[-1]["bottom"] = max(lines[-1]["bottom"], bottom)
        else:
            lines.append(
                {
                    "center": center,
                    "height": height,
                    "top": top,
                    "bottom": bottom,
                    "items": [(left, right, text)],
                }
            )
    rendered: list[str] = []
    for line in lines:
        items_on_line = sorted(line["items"], key=lambda item: item[0])
        text = "  ".join(piece for _left, _right, piece in items_on_line)
        left = items_on_line[0][0]
        right = max(item[1] for item in items_on_line)
        if _line_is_self_bubble(image_bgr, left, right, line["top"], line["bottom"], width):
            text = SIDE_SELF + text
        rendered.append(text)
    return "\n".join(rendered)


def rapid_engine() -> Callable[[Path], str]:
    try:
        import cv2
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise OcrUnavailable(
            "这个环境没装 RapidOCR。默认识图走站内 deepseek-flash，不必装 onnx。"
            "要回退：docker compose --profile ocr run --rm ocr research-ocr --engine rapidocr"
        ) from exc
    engine = RapidOCR()

    def recognize(path: Path) -> str:
        image_bgr = cv2.imread(str(path))
        result, _elapsed = engine(str(path))
        return order_text(result, image_bgr=image_bgr)

    return recognize


def vision_engine_id(settings=None) -> str:
    """写入 image_texts.engine 的名字。默认模型是 deepseek-flash，即 deepseek-flash-vision。"""
    from researchlib.deepseek import DEEPSEEK_MODEL_ID

    model = DEEPSEEK_MODEL_ID
    if settings is not None:
        from researchlib.llm import active_model_id

        model = active_model_id(settings)
    return f"{model}-vision"


def image_mime(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


def prepare_vision_image(image_bytes: bytes, mime: str) -> tuple[bytes, str]:
    """压到 MAX_VISION_SEND_BYTES 以内再送识图。返回 (bytes, mime)。

    优先 Pillow 缩长边并转 JPEG；没装 Pillow 且原图已超限则跳过，避免静默脏字。
    """
    if len(image_bytes) <= MAX_VISION_SEND_BYTES and (mime or "").lower() in {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/gif",
    }:
        # 仍可能 base64 后膨胀；JPEG/PNG 原图略大于阈值时也压一下更稳。
        if len(image_bytes) <= int(MAX_VISION_SEND_BYTES * 0.75):
            return image_bytes, mime
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError as exc:
        if len(image_bytes) > MAX_VISION_SEND_BYTES:
            raise OcrImageSkipped(
                f"超过 {MAX_VISION_SEND_BYTES} 字节且未装 Pillow，识图跳过"
            ) from exc
        return image_bytes, mime

    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
    except Exception as exc:  # noqa: BLE001 — 坏图当跳过
        raise OcrImageSkipped(f"图片打不开：{exc}") from exc
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")
    width, height = image.size
    longest = max(width, height) or 1
    scale = min(1.0, VISION_MAX_SIDE / longest)
    if scale < 1.0:
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
    quality = VISION_JPEG_QUALITY
    out = b""
    for _ in range(8):
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= MAX_VISION_SEND_BYTES:
            return out, "image/jpeg"
        width, height = image.size
        image = image.resize(
            (max(1, int(width * 0.85)), max(1, int(height * 0.85))),
            Image.Resampling.LANCZOS,
        )
        quality = max(40, quality - 5)
    if len(out) > MAX_VISION_SEND_BYTES:
        raise OcrImageSkipped(f"压缩后仍超过 {MAX_VISION_SEND_BYTES} 字节，识图跳过")
    return out, "image/jpeg"


def vision_messages(image_bytes: bytes, mime: str) -> list[dict]:
    """识图请求的 messages。content 是 text + image_url（data URL），由 llm.chat 原样 POST。"""
    payload = base64.b64encode(image_bytes).decode("ascii")
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{payload}"}},
            ],
        }
    ]


def normalize_vision_text(text: str) -> str:
    """去掉模型偶尔套上的代码围栏。说话人和界面字留给 wechat_dialogue。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def vision_engine(settings) -> Callable[[Path], str]:
    """站内识图。失败抛给 run_ocr，不在这里改走 RapidOCR。

    先压图再送 deepseek-flash，避免 en1 转发把大 data-url 掐断；瞬时网络错误最多再试两次。
    """
    import time

    from researchlib import llm

    def recognize(path: Path) -> str:
        raw = path.read_bytes()
        if len(raw) > MAX_VISION_BYTES:
            raise OcrImageSkipped(f"超过 {MAX_VISION_BYTES} 字节，识图跳过")
        payload, mime = prepare_vision_image(raw, image_mime(path))
        messages = vision_messages(payload, mime)
        last_exc: BaseException | None = None
        for attempt in range(VISION_TRANSIENT_TRIES):
            try:
                text, _model = llm.chat(settings, messages, exact_max_tokens=VISION_MAX_TOKENS)
                return normalize_vision_text(text)
            except llm.LLMError as exc:
                last_exc = exc
                detail = str(exc)
                transient = any(
                    needle in detail
                    for needle in (
                        "连不上",
                        "Remote end closed",
                        "DECRYPTION_FAILED",
                        "Bad MAC",
                        "timed out",
                        "Timeout",
                        "Connection reset",
                    )
                )
                if not transient or attempt + 1 >= VISION_TRANSIENT_TRIES:
                    raise
                time.sleep(1.5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    return recognize


def normalize_engine_choice(value: str | None) -> str:
    choice = (value or DEFAULT_ENGINE_CHOICE).strip().lower()
    if choice not in ENGINE_CHOICES:
        allowed = "、".join(ENGINE_CHOICES)
        raise ValueError(f"engine 只能是 {allowed}")
    return choice


def rapidocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def engine_problem(settings, choice: str | None = None) -> str:
    """开工前的阻碍。空字符串表示可以跑。文案不带凭证。"""
    from researchlib.jobs.base import credentials_problem

    picked = normalize_engine_choice(choice)
    if picked == "rapidocr":
        if rapidocr_available():
            return ""
        return "这个环境没装 RapidOCR。默认识图走 vision；回退要用 ocr 镜像并指定 engine=rapidocr。"
    missing = credentials_problem(settings)
    if picked == "vision":
        return missing
    # auto：有凭证用 vision，否则 RapidOCR。两个都没有才拦下。
    if not missing:
        return ""
    if rapidocr_available():
        return ""
    return missing


def resolve_engine(settings, choice: str | None = None) -> tuple[str, Callable[[Path], str]]:
    """返回 (写入 image_texts 的 engine 名, recognize)。

    vision 没有凭证就失败，不悄悄改走 RapidOCR。auto 才在没凭证时用 RapidOCR。
    单张 HTTP 失败不在这里回退，由 run_ocr 记 :error。
    """
    picked = normalize_engine_choice(choice)
    if picked == "rapidocr":
        return ENGINE, rapid_engine()
    from researchlib.llm import llm_configured

    if picked == "vision" or llm_configured(settings):
        if not llm_configured(settings):
            raise OcrUnavailable("没有配置 DEEPSEEK_API_KEY，识图起不来。要回退 RapidOCR 请显式 --engine rapidocr。")
        return vision_engine_id(settings), vision_engine(settings)
    return ENGINE, rapid_engine()


def _fatal_ocr(exc: Exception) -> bool:
    """整批停：没凭证，或接口拒绝鉴权。单张 5xx 不算。"""
    from researchlib.llm import LLMUnavailable

    if isinstance(exc, (OcrUnavailable, LLMUnavailable)):
        return True
    text = str(exc)
    return "接口返回 401" in text or "接口返回 403" in text


def pending_images(conn, limit: int, *, redo: bool = False, article_ids: list[int] | None = None):
    """还没识别过的本地图片，按引用它的文章从新到旧。重复文章里的图不算。

    redo 时连 image_texts 里已有的哈希也再认一遍。article_ids 只认这几篇挂着的图。
    engine 以 :error 结尾的行不算认完，不带 redo 也会重试；:skipped 和空正文的成功行不重试。
    """
    where = ["a.duplicate_of IS NULL"]
    params: list = []
    if article_ids:
        where.append("i.article_id = ANY(%s)")
        params.append(list(article_ids))
    if not redo:
        where.append(
            "NOT EXISTS (SELECT 1 FROM image_texts t WHERE t.content_hash = i.content_hash"
            " AND right(t.engine, 6) <> ':error')"
        )
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
    表格和长文保持原文。识图若已经带说话人，仍再剥界面字。阅读页用同一套规则。
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


def _touch_articles(conn, content_hash: str, text: str, touched: set[int], report: dict) -> None:
    for article in conn.execute(
        "SELECT DISTINCT article_id FROM article_images WHERE content_hash = %s", (content_hash,)
    ).fetchall():
        touched.add(article["article_id"])
        if text.strip() and replace_ocr_chunks(conn, article["article_id"]):
            report["articles"] += 1


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
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """识别一批图片，重切引用它们的文章的图片文字块。返回计数。

    单张失败记 ``{engine}:error`` 和空正文，不写入脏字。没凭证或 401/403 整批停。
    """
    report = {
        "images": 0,
        "with_text": 0,
        "failed": 0,
        "missing": 0,
        "articles": 0,
        "last_error": None,
    }
    touched: set[int] = set()

    def note() -> None:
        if on_progress is not None:
            on_progress(dict(report))

    try:
        for row in pending_images(conn, limit, redo=redo, article_ids=article_ids):
            if should_stop is not None and should_stop():
                break
            if deadline is not None and time.monotonic() > deadline:
                break
            path = Path(data_dir) / "images" / row["storage_path"]
            if not path.is_file():
                report["missing"] += 1
                note()
                continue
            used = engine
            if path.suffix.lower() not in OCR_EXTS:
                text = ""
                used = f"{engine}:skipped"
            else:
                try:
                    text = recognize(path)
                except OcrImageSkipped as exc:
                    logger.info("跳过图片 %s：%s", path, exc)
                    text = ""
                    used = f"{engine}:skipped"
                except Exception as exc:  # 单张图坏了不拖垮整批；鉴权失败除外
                    if _fatal_ocr(exc):
                        raise
                    logger.warning("图片识别失败 %s：%s", path, exc)
                    text = ""
                    used = f"{engine}:error"
                    report["failed"] += 1
                    report["last_error"] = str(exc)[:300]
            conn.execute(
                """
                INSERT INTO image_texts (content_hash, engine, text) VALUES (%s, %s, %s)
                ON CONFLICT (content_hash) DO UPDATE
                    SET engine = EXCLUDED.engine, text = EXCLUDED.text, created_at = now()
                """,
                (row["content_hash"], used, text or ""),
            )
            report["images"] += 1
            if text.strip():
                report["with_text"] += 1
            _touch_articles(conn, row["content_hash"], text or "", touched, report)
            conn.commit()
            note()
    finally:
        for article_id in sorted(touched):
            refresh_article_ocr(conn, article_id)
        if touched:
            conn.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了，2 引擎起不来（没凭证，或指定了没装的 RapidOCR）。"""
    parser = argparse.ArgumentParser(description="识别图片里的字，切成段落块进检索")
    parser.add_argument("--limit", type=int, default=500, help="这次最多识别多少张图")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停，0 表示不限")
    parser.add_argument("--redo", action="store_true", help="连已经识别过的图也再认一遍（重跑存量）")
    parser.add_argument("--article", action="append", type=int, default=[], help="只认这篇文章里的图，可重复")
    parser.add_argument(
        "--engine",
        choices=list(ENGINE_CHOICES),
        default=DEFAULT_ENGINE_CHOICE,
        help="vision=站内 deepseek-flash（默认）；rapidocr=回退；auto=有凭证用 vision，否则 RapidOCR",
    )
    args = parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    try:
        engine_name, recognize = resolve_engine(settings, args.engine)
    except (OcrUnavailable, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    conn = prepare(settings)
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes > 0 else None
    try:
        report = run_ocr(
            conn,
            settings.data_dir,
            args.limit,
            recognize,
            engine=engine_name,
            deadline=deadline,
            redo=args.redo,
            article_ids=args.article or None,
        )
    finally:
        conn.close()
    print(
        f"引擎 {engine_name}：识别 {report['images']} 张图，有字的 {report['with_text']} 张，"
        f"失败 {report['failed']} 张，文件缺失 {report['missing']} 张，"
        f"更新了 {report['articles']} 篇的图片文字"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

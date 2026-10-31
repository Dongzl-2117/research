"""图片里的字进检索（OCR / 识图）。

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。默认把本地图片交给
站内 deepseek-flash 识图（``llm.chat``，多模态 content），按内容哈希只认一次，结果存进
image_texts（engine 如 ``deepseek-flash-vision``）。识别出的字够多的图，切成段落块进检索
（article_chunks.lang = 'ocr'，标题「图片里的字」，锚点指向文章页上那张图）。
公司提及默认不看图片里的字，免得水印、表情包乱挂；正文短于 SHORT_BODY_CHARS 时例外，
截图帖要挂上图里的公司才能抽分家观点。

app 容器就能跑。单张识图失败记 ``engine`` 以 ``:error`` 结尾、正文为空，
下次不带 ``--redo`` 也会重试，不写进脏字。

    docker compose exec -T app research-ocr --limit 50

夜间任务设 NIGHTLY_OCR=1 时，在 app 容器里走识图补一批。
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

from researchlib.author_voice import site_visible_sql
from researchlib.chunks import replace_ocr_chunks
from researchlib.present_text import merge_wrapped_utterances, order_nested_replies
from researchlib.textutil import collapse_short_spam, fix_obvious_typos
from researchlib.vision_polish import repair_vision_misreads
from researchlib.wechat_dialogue import (
    drop_broadcast_chrome,
    present_ocr_pieces,
    present_stored_ocr,
)

logger = logging.getLogger("researchlib.ocr")

OCR_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
# 识图请求体太大时跳过，不反复打接口。手机截图远小于这个。
MAX_VISION_BYTES = 10 * 1024 * 1024
# 经 mini 上 DeepSeek en1 转发时，过大的 data-url 会在 TLS 中途被掐断。
# 压到这个字节数以下再 base64，payload 大约 150KB 内，和问库分批上限同一量级。
MAX_VISION_SEND_BYTES = 280_000
VISION_MAX_SIDE = 1920
VISION_JPEG_QUALITY = 85
VISION_TRANSIENT_TRIES = 3
VISION_MAX_TOKENS = 32768  # 全量 VISION_PROMPT+thinking 下 8k/16k 会把正文挤空；32k 实测有字且不 OOM
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
- 气泡里嵌套的截图、估值表、PE 表、长文卡片必须整段转写，标成「网友：」，禁止因字小整段丢掉。
- 嵌套表按行输出，指标与数字同一行（如「16倍 约133港元」）。
- 嵌套图里的季度（Q1/Q2/Q3）、同比正负号、价格区间必须与图内一致，禁止改写或脑补长句。
- 剥掉界面字，不要输出：可按住转文字、回到引用位置、设为群待办、
  继续提问、AI搜索、状态栏时间、群名、雪球、LIVE、加人会员群、小程序卡片、输入框。
- 截到屏幕边缘的半截气泡：能认的照写，认不全写 … 或 ?；禁止把半截尾巴编成公司名或新句，
  也禁止用本图其它气泡里出现过的公司名补全（例如不要把「…各种/各自的 IP 业务」写成「名创优品…」）。
- 「一年后」「两年后」等常见时间词，中间的「年」看不清也优先写「年」，不要写成「一?后」「一?会」。
- 知识星球「扫码领取优惠券」「立减金」「仅限前 N 名」推广卡：不要输出任何字（包括说明句），也不要标成「方伟：」。
- 微博/新闻评论卡（昵称+地区+数字，或「xx新闻联播」条）整段标「网友：」，不要留真实昵称。
- 视频号/公众号关注条不要输出：已关注、点击 #话题 查看更多、欢迎关注 @账号 视频号、作者提示: 内容为转载。
- 雪球/微博帖头与壳不要输出：发帖昵称、地区、影响力徽章（如十大影响力用户、超级某某）、
  「发讨论」「查看对话」、点赞评论转发数字栏。
- 「回复@甲:… //@乙:回复@丙:…」去掉昵称和「回复@」「//@」壳。
  按时间从早到晚各写一行「网友：」（越靠后的 //@ 越早），不要只留最上面那条新回复。
- 雪球、微博这类嵌套回复：视觉上靠上的一块常常是较晚的回复，
  下面缩进或灰底引用卡片是被回复的更早发言。不要按从上到下当成时间顺序。
  先写较早的发言，再写回复。界面昵称一律「网友：」；
  引语里的说话人留在正文里（如「莫尼什：我是苹果用户……」）。
  例：上面是「李录投资也很好……」，中间是「大道会不会考虑和李录做一期对谈节目？」，
  下面卡片是「莫尼什：我是苹果用户……」。应写成：
  网友：莫尼什：我是苹果用户……
  网友：大道会不会考虑和李录做一期对谈节目？
  网友：李录投资也很好……
- 杂志/品牌封面壳不要输出：领教工坊、INSIDE THE BEST、COLMO 等刊头、Logo 字、品牌水印；
  「来源：思想×岭南|…」这类刊源行也不要。封面大字若是正文金句（如「光做家电没有未来」）照写。
- 同一短句连续刷屏（如「深刻反省」重复十几遍）只保留一次，不要整段复读。
- 视频画面上的角标和主播条不要输出：未成年人请勿饮酒、主播或主持人加名字、底部节目名水印。
  新闻标题和数字照写。
- 品牌广告海报只留条款和数字：单独一行的 Logo、四字对仗口号不要输出。保障条款、适用条件照写。
  书籍目录照写，不要当成界面删掉。
- 上下文已经把词定死时，识图滑字直接写成对的词，不要照抄错字，也不要凭空改写。
  例如：家电品牌并列（格力、海尔、空调、冰箱）里的「墓地」写成「美的」；
  free cash flow 的中间词只错一个字母（如 free case flow）写成 free cash flow。
  没有这种上下文就照写（去墓地扫墓、单独的 case 不改）。看不清仍写 ?，禁止改数字和观点。
- 看不清的字写 ?，禁止编造。

若是表格或柱状图：
- 公司与数字一一对应，保留行列（公司、指标、数字写在同一行）。
- 看不清写 ?，禁止编造数字或公司名。

分段看画面，由你判断，不要套固定段数。
- 画面上是一块的写成一段，段与段之间空一行。同一句被屏幕截断时写成一句，
  例如「企业家精神是敢于在最顺的时候」和下一行「对自己动刀子」是一句。
- 画面上分开的（大标题、字幕、图下说明、另一句话）各自成段，空行隔开。
- 不要把每个短行都单独成段，也不要把整张图合成一段。
- 微信或星球气泡仍是一条气泡一行，不要把不同气泡粘成一段。

其它截图按阅读顺序转写，分段同样看画面。看不清写 ?，禁止编造。不要额外评论。
"""


class OcrUnavailable(Exception):
    pass


class OcrImageSkipped(Exception):
    """这张图不再试（太大）。run_ocr 记 :skipped，不占重试。"""


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


_COUPON_MARK = re.compile(r"扫码领取优惠券|立减金|仅限前\s*\d+\s*名")
_YEAR_TYPO = (
    (re.compile(r"一\?会"), "一年后会"),
    (re.compile(r"一\?后"), "一年后"),
)
# 杂志/品牌封面壳（整行丢掉）
_BRAND_MASTHEAD = re.compile(
    r"^(?:"
    r"领教工坊"
    r"|INSIDE\s+THE\s+BEST"
    r"|COLMO"
    r")$",
    re.I,
)
_SOURCE_MASTHEAD = re.compile(
    r"^来源\s*[:：]\s*(?:思想[?？×xX]?岭南|领教工坊)\b"
)
_SHORT_REPEAT_TOKEN = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]{2,12}$")


def _collapse_repeated_short_phrases(line: str) -> str:
    """「深刻反省，深刻反省，…」压成一次；跨逗号/顿号的短词连刷。"""
    raw = (line or "").rstrip()
    if not raw:
        return ""
    prefix = ""
    labeled = re.match(r"^((?:方伟|网友)\s*[:：]\s*)", raw)
    if labeled:
        prefix = labeled.group(1)
        raw = raw[labeled.end() :]
    parts = re.split(r"[，,、；;]+", raw)
    tokens = [p.strip() for p in parts if p.strip()]
    if len(tokens) >= 3 and all(_SHORT_REPEAT_TOKEN.fullmatch(t) for t in tokens):
        if len(set(tokens)) == 1:
            return f"{prefix}{tokens[0]}".strip()
        kept: list[str] = []
        for token in tokens:
            if kept and kept[-1] == token:
                continue
            kept.append(token)
        if len(kept) < len(tokens):
            return f"{prefix}{'，'.join(kept)}".strip()
    return f"{prefix}{raw}".strip() if prefix else line.rstrip()


def _is_brand_chrome_line(line: str) -> bool:
    body = re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", (line or "").strip())
    if not body:
        return False
    if _BRAND_MASTHEAD.fullmatch(body):
        return True
    if _SOURCE_MASTHEAD.match(body):
        return True
    return False


def normalize_vision_text(text: str) -> str:
    """去掉模型偶尔套上的代码围栏；半截尾巴里乱编的公司名留给 prompt 约束，这里只做轻清理。

    说话人和界面字留给 wechat_dialogue。推广卡、一年后误写成一?、品牌刊头、短句刷屏在这里先收掉。
    只把半句折行接上，模型用空行分开的段保留。雪球嵌套回复改成从早到晚。识图滑字仍交给 vision_polish。
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    for pat, repl in _YEAR_TYPO:
        raw = pat.sub(repl, raw)
    # 整张都是优惠券推广卡，或模型写了「按规则不输出」说明句：直接丢掉。
    if re.search(r"按规则整段不输出|优惠券推广卡", raw):
        return ""
    coupon_hits = _COUPON_MARK.findall(raw)
    if coupon_hits and len(re.findall(r"[一-鿿]", raw)) <= 80:
        if re.search(r"扫码领取优惠券|立减金", raw):
            return ""
    # 模型偶发把截断气泡收成「某某公司?」或用上文公司名补半截尾巴。
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,8}\?", s) and ("方伟" not in s and "网友" not in s):
            if "：" not in s and ":" not in s:
                continue
        # 单行优惠券话术（含已标方伟的）丢掉
        body = re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", s)
        if _COUPON_MARK.search(body) and len(body) <= 40:
            continue
        if re.search(r"按规则整段不输出|优惠券推广卡", body):
            continue
        # 视频号/雪球壳轻清理（wechat_dialogue 还会再剥一轮）
        if re.fullmatch(r"(?:[\u4e00-\u9fffA-Za-z0-9_\-]{2,20}\s+)?已关注", body):
            continue
        if re.search(r"欢迎关注\s*@?\S{0,40}\s*视频号|点击\s*#\S{1,40}\s*查看更多|作者提示\s*[:：]\s*内容为转载", body):
            continue
        if re.fullmatch(r"(?:发讨论|查看对话)[>〉›]?", body):
            continue
        if re.fullmatch(r"\d{1,6}(?:\s+\d{1,6}){1,4}", body):
            continue
        if _is_brand_chrome_line(line):
            continue
        collapsed = _collapse_repeated_short_phrases(line.rstrip())
        if not collapsed:
            continue
        lines.append(collapsed)
    # 去掉账号栏后只剩优惠券日期/名额的尾巴
    kept = []
    for line in lines:
        s = line.strip()
        body = re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", s)
        if re.fullmatch(r"仅限前\s*\d+\s*名", body):
            continue
        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}\s*至\s*\d{4}/\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}", body):
            continue
        if re.fullmatch(r"\d+\s*元立减金。?", body):
            continue
        if re.fullmatch(r"方伟看十年|看十年", body):
            continue
        kept.append(line)
    lines = kept
    # 连续相同短行（跨行刷「深刻反省」）只留一行
    deduped: list[str] = []
    for line in lines:
        body = re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", line.strip())
        prev = (
            re.sub(r"^(?:方伟|网友)\s*[:：]\s*", "", deduped[-1].strip()) if deduped else None
        )
        if deduped and body and body == prev and _SHORT_REPEAT_TOKEN.fullmatch(body):
            continue
        deduped.append(line)
    lines = drop_broadcast_chrome(deduped)
    if lines:
        last = lines[-1].strip()
        # 「方伟：名创优品… / 名创优品业务…」——半截尾巴常见编造
        if re.search(r"名创优品", last) and re.search(r"[…\.．?？]+$", last):
            if last.startswith("方伟：") or last.startswith("方伟:"):
                lines[-1] = "方伟：…"
            elif "：" not in last and ":" not in last:
                lines.pop()
    text = order_nested_replies(merge_wrapped_utterances("\n".join(lines).strip()))
    return repair_vision_misreads(fix_obvious_typos(collapse_short_spam(text)))


def vision_engine(settings) -> Callable[[Path], str]:
    """站内识图。失败抛给 run_ocr，记成 :error。

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


def resolve_engine(settings) -> tuple[str, Callable[[Path], str]]:
    """返回 (写入 image_texts 的 engine 名, recognize)。没有凭证就起不来。

    单张 HTTP 失败不在这里处理，由 run_ocr 记 :error。
    """
    from researchlib.llm import llm_configured

    if not llm_configured(settings):
        raise OcrUnavailable("没有配置 DEEPSEEK_API_KEY，识图起不来。")
    return vision_engine_id(settings), vision_engine(settings)


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
    不点名文章时，只挂在知识星球读者帖、「图片」评论壳、停用来源下的图不认：网站上不展示，认了白花钱。
    """
    where = ["a.duplicate_of IS NULL"]
    params: list = []
    if article_ids:
        where.append("i.article_id = ANY(%s)")
        params.append(list(article_ids))
    else:
        where.append(site_visible_sql("a"))
        where.append("EXISTS (SELECT 1 FROM sources s WHERE s.id = a.source_id AND s.enabled)")
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
    engine: str | None = None,
    deadline: float | None = None,
    *,
    redo: bool = False,
    article_ids: list[int] | None = None,
) -> dict:
    """识别一批图片，重切引用它们的文章的图片文字块。返回计数。

    单张失败记 ``{engine}:error`` 和空正文，不写入脏字。没凭证或 401/403 整批停。
    engine 是写进 image_texts 的名字，不给就用默认的 deepseek-flash-vision。
    """
    engine = engine or vision_engine_id()
    report = {
        "images": 0,
        "with_text": 0,
        "failed": 0,
        "missing": 0,
        "articles": 0,
        "last_error": None,
        "followups": {"digest": [], "zsxq_title": []},
        "ocr_changed": [],
    }
    touched: set[int] = set()
    try:
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
    finally:
        changed: list[dict] = []
        for article_id in sorted(touched):
            before = _ocr_snapshot(conn, article_id)
            new_hash = refresh_article_ocr(conn, article_id)
            if before is None:
                continue
            old_hash = before.get("ocr_hash") or ""
            if not new_hash or new_hash == old_hash:
                continue
            after = _ocr_snapshot(conn, article_id) or before
            body_len = after.get("body_len")
            if body_len is None:
                body_len = before.get("body_len") or 0
            changed.append(
                {
                    "id": int(article_id),
                    "source_id": after.get("source_id") or before.get("source_id") or "",
                    "body_len": int(body_len),
                    "old_hash": old_hash,
                    "new_hash": new_hash,
                }
            )
        if changed:
            try:
                from researchlib.followups import note_ocr_changes

                report["followups"] = note_ocr_changes(conn, changed)
                report["ocr_changed"] = [int(row["id"]) for row in changed]
            except Exception:
                logger.exception("识图后续没排上")
        if touched:
            conn.commit()
    return report


def _ocr_snapshot(conn, article_id: int):
    """识图写回前后对照用。正文长度决定要不要重排摘要和标题。"""
    return conn.execute(
        """
        SELECT id, source_id, ocr_hash, length(coalesce(body_text, '')) AS body_len
        FROM articles WHERE id = %s
        """,
        (article_id,),
    ).fetchone()


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了，2 识图起不来（没配 DEEPSEEK_API_KEY）。"""
    parser = argparse.ArgumentParser(description="识别图片里的字，切成段落块进检索")
    parser.add_argument("--limit", type=int, default=500, help="这次最多识别多少张图")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停，0 表示不限")
    parser.add_argument("--redo", action="store_true", help="连已经识别过的图也再认一遍（重跑存量）")
    parser.add_argument("--article", action="append", type=int, default=[], help="只认这篇文章里的图，可重复")
    # 只剩 vision。留着这个参数，以前写了 --engine vision 的命令（scripts/nightly.sh）照样能跑。
    parser.add_argument("--engine", choices=["vision"], default="vision", help="站内 deepseek-flash 识图")
    args = parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    try:
        engine_name, recognize = resolve_engine(settings)
    except OcrUnavailable as exc:
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
        follow = report.get("followups") or {}
        n_digest = len(follow.get("digest") or [])
        n_title = len(follow.get("zsxq_title") or [])
        if n_digest or n_title:
            print(f"短正文图片文字变了：摘要重排 {n_digest} 篇，标题重排 {n_title} 篇")
            from researchlib.followups import drain_ocr_followups

            drained = drain_ocr_followups(conn, settings, follow)
            print(f"已重写摘要 {drained['digest']} 篇，标题 {drained['titles']} 篇")
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

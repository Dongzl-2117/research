"""图片里的字进检索（OCR / 识图）。

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。默认把本地图片交给
站内 deepseek-flash 识图（``llm.chat``，多模态 content），按内容哈希只认一次，结果存进
image_texts（engine 如 ``deepseek-flash-vision``）。识别出的字够多的图，切成段落块进检索
（article_chunks.lang = 'ocr'，标题「图片里的字」，锚点指向文章页上那张图）。
公司提及默认不看图片里的字，免得水印、表情包乱挂；正文短于 SHORT_BODY_CHARS 时例外，
截图帖要挂上图里的公司才能抽分家观点。

识图交回结构（JSON，见 ``researchlib.ocr_units``）：谁说的、是不是截图主人、引用的哪条、哪条被图片边缘截断，
都由看得见图的模型标好，存进 image_texts.units，后面只排版和拼接，不再按规则猜。
units 为空的是以前的自由文字，仍由 wechat_dialogue 的旧规则整理（冻结，不再往里加）。

app 容器就能跑。单张识图失败记 ``engine`` 以 ``:error`` 结尾、正文为空，
下次不带 ``--redo`` 也会重试，不写进脏字。JSON 连着两次读不出来记 ``:invalid``，
不自动重试（重试又要花钱），``--redo --article`` 点名重认。

每行记着按第几版识图说明认的（image_texts.prompt_version）。不点名文章的 ``--redo`` 只重认版本比
VISION_PROMPT_VERSION 旧的图，停了再开从没认完的接着认；``--limit 0`` 只报还剩多少张。
自动上线在等时（status/.deploy-waiting），不点名文章的识图认完手上这张就停，下一次接着认。

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
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.types.json import Jsonb

from researchlib.author_voice import site_visible_sql
from researchlib.chunks import replace_ocr_chunks
from researchlib.ocr_units import (
    ImageText,
    UnitsError,
    format_blocks,
    format_units,
    has_dialogue,
    parse_reply,
    stitch,
)
from researchlib.present_text import merge_wrapped_utterances, order_nested_replies
from researchlib.textutil import collapse_short_spam, fix_obvious_typos
from researchlib.vision_polish import repair_vision_misreads
from researchlib.wechat_dialogue import (
    attribution_uncertain,
    drop_broadcast_chrome,
    present_ocr_pieces,
    present_stored_ocr,
    speaker_note,
    speaker_prefix,
    split_speaker,
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
VISION_MAX_TOKENS = 65536  # 全量 VISION_PROMPT+thinking 下 8k/16k 会把正文挤空，32k 仍偶发；64k 配合 llm 的空正文重试
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

# 识图只交结构，不按 App 列界面字、不按案例补规则：遇到新版式改这里的通用说明（见 researchlib.ocr_units）。
VISION_PROMPT = """\
你在把投资研究文章里的一张图转成结构化的文字，给检索、摘要和观点用。\
只输出一个 JSON 对象，不要 markdown 代码块，不要解释。

格式：{"kind": "…", "blocks": […]}

kind 写这张图是什么：chat（聊天记录）、post（帖子、评论和回复）、\
text（成段的文字：文章、新闻、书页、公告、幻灯片）、table（表格）、chart（图表）、\
photo（照片、表情包等没有可读文字的图）、other。

blocks 按阅读顺序排，每块是下面一种：
- 消息：{"type": "message", "speaker": "显示名", "self": false, "text": "这条说的话"}
  聊天、帖子、评论里一个人发的一条。
  speaker 照图上显示的名字抄，表情、国旗、符号也照抄；看不到名字写空串，不要猜，也不要改成「网友」这类称呼。
  self：只有聊天界面里截图的人自己发的写 true，也就是靠右的（常是绿色的）气泡，这种气泡旁边一般没有名字。\
其余都写 false，包括帖子、评论和回复：名字旁标着作者、楼主的，是那个帖子的作者，不一定是截图的人，名字照抄在 speaker。
  有引用或回复时加 "quote": {"speaker": "…", "self": false, "text": "被引用的那条"}，\
引用框、灰色卡片、「回复@某某：」后面的原话都算。\
引用框归和它挨得最近、在同一侧对齐的那条消息：在那个气泡里面，或者紧贴在它的上面或下面。\
不要只因为引用框排在某条消息上面，就当成是那条消息引用的。
  被图片上下边缘截掉一部分（或被顶上的标题栏、底下的输入框挡住一部分）的那条加 "cut": "top" 或 "bottom"，\
只写看得到的字，不要补全，也不要照上下文猜成一句话；整条的字都只露出半截、一个也认不全的，这条不写。
  消息里嵌着的截图、表格、文章卡片，把里面的字写进 text，表格一行写一行。
- 段落：{"type": "text", "text": "…"}。成段的文字一段一块，标题、正文段落、图注各自成块。\
被图片边缘截断的段落同样加 "cut"。
- 表格：{"type": "table", "rows": [["…", "…"], …]}。一行一个数组，有表头放第一行。\
图表把图上标出来的数据按系列或类目写成行，图上没标的数不要估。
每块都可以加 "marks": ["…"]：截图以后有人另外画上去的圈、框、箭头、划线、荧光笔标出了这一块里的哪些字，\
照抄那几处，一处一项。后画的标注笔画不规整，颜色常和版面不搭（多是红色），常压在字上或画出版面；\
版面本来就有的下划线、虚线、链接线、加粗、颜色和底色，界面上的高亮、选中和红点，都不算。
字明显比这张图上的正文小的块（比如聊天里转发的书页、文章、论坛截图）加 "small": true，\
小字照样逐字抄，认不准的字写 ?。

怎么写：
1. 只写图里的内容，界面一概不写。内容是人说的话、文章正文、表格和图表里的字和数字、图片本身印着的字，\
还有帖子、推文、文章署的发布日期（单独写一块段落）。\
界面是状态栏、App 和群的名字、按钮、输入框、菜单和标签栏、聊天消息和评论旁的时间、已读和点赞评论转发收藏的数字、\
头像旁的徽章和地区、系统提示（谁加入了群、撤回了消息、拍了拍）、广告和推广引流卡片（优惠券、加群、关注、二维码）、\
水印和 Logo。拿不准时想一想：读者转述这张图时会不会说这句话，不会说的就不写。
2. 消息按发言先后排。聊天从上往下；回复链里被回复的那条更早，写进 quote 或排在前面。
3. 屏幕宽度造成的折行接成一句；画面上本来分开的段落分成几块。同一句话刷屏只写一次。
4. 照着图逐字抄，不要改。看得清的字原样写：作者的错别字、重复的字、不通顺的句子、少见的名字和拼写都照抄，\
不要改成你觉得对的写法；数字、正负号、单位、年份和季度照图上写。
5. 看不清的字写 ?，不要编：字太小、模糊、只露出半截都算。宁可多写几个 ?，也不要写出一句通顺但图上没有的话。\
不要用图里别处出现过的词补，也不要拿你自己知道的说法（书里的原文、商品名、公司名）补。\
只有笔画模糊、几个字形相近的字都像时，才挑上下文说得通的那个。
6. 没有可读文字的图，blocks 写 []。

例子（王小明发的一条被图片上边缘截掉了开头；截图的人在右边引用它回了一句）：
{"kind": "chat", "blocks": [\
{"type": "message", "speaker": "王小明", "self": false, "text": "毛利率35%，比去年高", "cut": "top"}, \
{"type": "message", "speaker": "", "self": true, "text": "先看现金流", \
"quote": {"speaker": "王小明", "self": false, "text": "毛利率35%，比去年高"}}]}
"""
# 上面这份说明的版本，写进 image_texts.prompt_version。改了说明、要让以前认过的图按新说明重认时加一：
# 不点名文章的 --redo 只重认版本比这个旧的图。措辞小改、不值得花钱重认的不加。
# 1：11-07 交回结构。2：11-08 拿 30 张旧图对比后改的：看得清的照抄不改字，看不清的写 ? 不编，
# 帖子的发布日期要写，后画的圈和划线标出的字写进 marks。
# 3：11-08 第二次对比以后改的：引用框归挨得最近、同一侧对齐的那条消息；字明显比正文小的块标 small；
# 版面本来就有的下划线、虚线不算后画的标注。
VISION_PROMPT_VERSION = 3


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
    prefix = speaker_prefix(raw)
    if prefix:
        raw = raw[len(prefix) :]
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


def _body_of(line: str) -> str:
    """去掉行首说话人（方伟、网友或截图网名）后剩下的字。"""
    speaker = split_speaker(line, names=True)
    return speaker[1] if speaker else (line or "").strip()


def _is_brand_chrome_line(line: str) -> bool:
    body = _body_of(line)
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
        body = _body_of(s)
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
        body = _body_of(s)
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
        body = _body_of(line)
        prev = _body_of(deduped[-1]) if deduped else None
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


@dataclass
class Recognition:
    """一张图识出来的东西。units 为空是模型没按结构交（旧格式的文字），error 非空是结构读不出来。"""

    text: str
    units: dict | None = None
    usage: dict = field(default_factory=dict)
    error: str = ""


# 模型回了 JSON 却读不出来时，同一张图最多再要这么多次。还不行记 :invalid，不再自动重试，免得每晚为它花钱。
VISION_UNITS_RETRIES = 1


def read_vision_reply(reply: str) -> tuple[str, dict | None]:
    """模型的回答 → (存进 image_texts.text 的字, units)。

    按结构交的：字由 units 排出来（读旧列的地方照样能用），units 存原样结构。
    没按结构交的（自由文字）：照旧经 normalize_vision_text 轻清理，units 为空，后面走旧规则。
    像 JSON 却读不出来的抛 UnitsError。
    """
    units = parse_reply(reply)
    if units is None:
        return normalize_vision_text(reply), None
    return format_units(units), units


def vision_engine(settings) -> Callable[[Path], Recognition]:
    """站内识图。失败抛给 run_ocr，记成 :error。

    先压图再送 deepseek-flash，避免 en1 转发把大 data-url 掐断；瞬时网络错误最多再试两次。
    回答像 JSON 却读不出来（多半是截断了），再要一次；还不行交回 error，run_ocr 记 :invalid。
    """
    import time

    from researchlib import llm

    def ask(messages: list[dict], usage: dict) -> str:
        last_exc: BaseException | None = None
        for attempt in range(VISION_TRANSIENT_TRIES):
            try:
                text, _model = llm.chat(settings, messages, exact_max_tokens=VISION_MAX_TOKENS, usage=usage)
                return text
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

    def recognize(path: Path) -> Recognition:
        raw = path.read_bytes()
        if len(raw) > MAX_VISION_BYTES:
            raise OcrImageSkipped(f"超过 {MAX_VISION_BYTES} 字节，识图跳过")
        payload, mime = prepare_vision_image(raw, image_mime(path))
        messages = vision_messages(payload, mime)
        usage: dict = {}
        problem = ""
        for _attempt in range(1 + VISION_UNITS_RETRIES):
            try:
                text, units = read_vision_reply(ask(messages, usage))
            except UnitsError as exc:
                problem = str(exc)
                logger.info("识图结构读不出来 %s：%s", path, exc)
                continue
            return Recognition(text, units, usage)
        return Recognition("", None, usage, problem)

    return recognize


def resolve_engine(settings) -> tuple[str, Callable[[Path], Recognition]]:
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


def _pending_where(*, redo: bool, article_ids: list[int] | None) -> tuple[list[str], list]:
    """要认的图的筛选条件（article_images i JOIN articles a 上）。见 ``pending_images``。"""
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
    elif not article_ids:
        # 全库重认只认按旧说明认的。以前每次都从最新的图从头认，停了再开就把刚认过的再付一次钱。
        where.append(
            "NOT EXISTS (SELECT 1 FROM image_texts t WHERE t.content_hash = i.content_hash"
            " AND right(t.engine, 6) <> ':error' AND t.prompt_version >= %s)"
        )
        params.append(VISION_PROMPT_VERSION)
    return where, params


def pending_images(conn, limit: int, *, redo: bool = False, article_ids: list[int] | None = None):
    """还没识别过的本地图片，按引用它的文章从新到旧。重复文章里的图不算。

    redo 时连 image_texts 里已有的哈希也再认一遍：点名文章（article_ids）的全部重认；不点名时只认
    按旧说明认的（prompt_version 比 VISION_PROMPT_VERSION 旧或为空），按现在的说明认过的不再认。
    engine 以 :error 结尾的行不算认完，不带 redo 也会重试；:skipped、:invalid 和空正文的成功行不重试。
    不点名文章时，只挂在知识星球读者帖、「图片」评论壳、停用来源下的图不认：网站上不展示，认了白花钱。
    """
    where, params = _pending_where(redo=redo, article_ids=article_ids)
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


def redo_remaining(conn) -> dict:
    """不点名文章的 --redo 还要认多少张图，其中多少张以前认出过字（没认出字的多半是照片和装饰图）。"""
    where, params = _pending_where(redo=True, article_ids=None)
    row = conn.execute(
        f"""
        SELECT count(*) AS images, count(*) FILTER (WHERE had_text) AS with_text
        FROM (
            SELECT i.content_hash, bool_or(coalesce(btrim(t.text), '') <> '') AS had_text
            FROM article_images i
            JOIN articles a ON a.id = i.article_id
            LEFT JOIN image_texts t ON t.content_hash = i.content_hash
            WHERE {" AND ".join(where)}
            GROUP BY i.content_hash
        ) pending
        """,
        tuple(params),
    ).fetchone()
    return {"images": int(row["images"] or 0), "with_text": int(row["with_text"] or 0)}


def useful_image_text(text: str) -> bool:
    """汉字、字母、数字够 READING_MIN_CHARS 个，才算图里有可读的字。

    空白、水印、几个字的装饰不算。刚过线的拿不准，算有用，不能漏。
    不按「是不是微信、是不是表格」分：有可读的字就留。
    """
    return len(_MEANINGFUL.findall(text or "")) >= READING_MIN_CHARS


def compose_ocr_text(pieces: list, owners: tuple[str, ...] = ()) -> str:
    """按图片顺序拼成观点和摘要能读的旁路正文。

    pieces 是每张图的识别结果：字符串（旧格式），或 ``ImageText``（带 units 的是结构化的）。
    owners 是这篇来源作者本人的名字（``article_owners``）：结构化的图里显示名是这些账号的，写成作者。
    太短的（水印、表情）丢掉。这份字给摘要、观点、问库和检索；阅读页不铺进正文，只留原图。

    全是旧格式时和以前一字不差：微信群聊截图整理成「方伟：」「网友：」，表格和长文保持原文，
    识图若已经带说话人仍再剥界面字（wechat_dialogue，冻结）。
    有结构化的图时，结构化的那几张只排版、按首尾重合拼接（``ocr_units``），不再过旧规则；
    夹在中间的旧格式图仍按旧规则整理，前后顺序不变。
    """
    records = [piece if isinstance(piece, ImageText) else ImageText("", piece or "", None) for piece in pieces]
    if not any(record.units is not None for record in records):
        useful = [text for text in ((record.text or "").strip() for record in records) if useful_image_text(text)]
        if not useful:
            return ""
        return present_ocr_pieces(useful)
    return "\n\n".join(
        text for _record, text in _structured_pieces(records, legacy_runs=True, owners=owners) if text
    )


def _structured_pieces(
    records: list[ImageText], *, legacy_runs: bool, owners: tuple[str, ...] = ()
) -> list[tuple[ImageText, str]]:
    """有结构化图的一篇：[(那张图, 整理好的字)]。同一张图出现两次只算第一次。

    结构化的图按首尾重合拼接后排版，引用的那条前面写过就不再写。
    legacy_runs：连着的几张旧格式图一起交给旧规则（拼旁路正文时，旧规则要把连着的群聊合成一块），
    这一串的字挂在第一张上；为 False 时一张一张整理（检索块一张图一块）。两种都共用一份「写过的句子」去重。
    """
    seen_digests: set[str] = set()
    ordered: list[ImageText] = []
    for record in records:
        if record.digest and record.digest in seen_digests:
            continue
        if record.digest:
            seen_digests.add(record.digest)
        ordered.append(record)
    stitched = stitch([record.units for record in ordered])
    found: list[tuple[ImageText, str]] = []
    written: set[str] = set()
    seen: list[str] = []
    run: list[ImageText] = []

    def flush() -> None:
        if not run:
            return
        useful = [record.text.strip() for record in run if useful_image_text(record.text or "")]
        text = present_ocr_pieces(useful, seen).strip() if useful else ""
        found.append((run[0], text))
        run.clear()

    for record, blocks in zip(ordered, stitched, strict=True):
        if blocks is None:
            run.append(record)
            if not legacy_runs:
                flush()
            continue
        flush()
        text = format_blocks(blocks, written, owners)
        found.append((record, text if useful_image_text(text) else ""))
    flush()
    return found


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


def article_image_texts(conn, article_id: int) -> list[ImageText]:
    """这篇已识别的图，按在正文里的先后。"""
    rows = conn.execute(
        """
        SELECT i.content_hash, t.text, t.units
        FROM article_images i
        JOIN image_texts t ON t.content_hash = i.content_hash
        WHERE i.article_id = %s
        ORDER BY i.id
        """,
        (article_id,),
    ).fetchall()
    return [ImageText(row["content_hash"], row["text"] or "", row["units"]) for row in rows]


def article_owners(conn, article_id: int) -> tuple[str, ...]:
    """这篇来源的作者本人在各平台的名字：sources.author 加 poster_names（启动时从来源配置同步）。"""
    row = conn.execute(
        "SELECT s.author, s.poster_names FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.id = %s",
        (article_id,),
    ).fetchone()
    if row is None:
        return ()
    return tuple(name for name in (row["author"], *(row["poster_names"] or ())) if name)


def refresh_article_ocr(conn, article_id: int) -> str:
    """按这篇已识别的图重写 articles.ocr_text / ocr_hash。返回哈希。"""
    images = article_image_texts(conn, article_id)
    owners = article_owners(conn, article_id) if any(image.units is not None for image in images) else ()
    text = compose_ocr_text(images, owners)
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
    有结构化的图时，重写出来的就是定稿：说话人是识图标好的，不再拿旧规则过一遍。
    """
    images = article_image_texts(conn, article_id)
    if images:
        refresh_article_ocr(conn, article_id)
    row = conn.execute(
        "SELECT ocr_text, ocr_hash FROM articles WHERE id = %s",
        (article_id,),
    ).fetchone()
    if row is None:
        return "", ""
    if any(image.units is not None for image in images):
        return row["ocr_text"] or "", row["ocr_hash"] or ""
    cleaned = present_stored_ocr(row["ocr_text"] or "")
    digest = hash_ocr(cleaned)
    if cleaned != (row["ocr_text"] or "") or digest != (row["ocr_hash"] or ""):
        conn.execute(
            "UPDATE articles SET ocr_text = %s, ocr_hash = %s WHERE id = %s",
            (cleaned, digest, article_id),
        )
    return cleaned, digest


def ocr_speaker_note(conn, article_id: int, text: str, host: str) -> str:
    """摘要和观点提示里那句说话人说明（``speaker_note``）。text 是已经换成作者名的图片文字。

    全是旧格式的图：照旧从文字里判断有没有对话、冒号前的名字拿不拿得准。
    有结构化的图：对话是识图标好的，有消息就算有对话；只有旧格式的那几张还按旧规则看边界拿不拿得准。
    """
    images = article_image_texts(conn, article_id)
    if not any(image.units is not None for image in images):
        return speaker_note(text, host)
    legacy = [image.text for image in images if image.units is None]
    dialogue = any(has_dialogue(image.units) for image in images) or any(
        split_speaker(line, names=True) for piece in legacy for line in piece.splitlines()
    )
    uncertain = any(attribution_uncertain(piece) for piece in legacy)
    return speaker_note(text, host, dialogue=dialogue, uncertain=uncertain)


def _touch_articles(conn, content_hash: str, text: str, touched: set[int], report: dict) -> None:
    for article in conn.execute(
        "SELECT DISTINCT article_id FROM article_images WHERE content_hash = %s", (content_hash,)
    ).fetchall():
        touched.add(article["article_id"])
        if text.strip() and replace_ocr_chunks(conn, article["article_id"]):
            report["articles"] += 1


def recognize_one(recognize, path: Path) -> tuple[Recognition | None, Exception | None, float]:
    """在工作线程里认一张图：只调模型，不碰库。交回（结果，出的错，秒数），出错时结果为空。"""
    started = time.monotonic()
    try:
        found = recognize(path)
    except Exception as exc:  # 交给主线程判断：跳过、单张失败，还是整批停
        return None, exc, time.monotonic() - started
    result = found if isinstance(found, Recognition) else Recognition(found or "")
    return result, None, time.monotonic() - started


def _outcome(engine: str, path: Path, future, report: dict) -> tuple[str, Recognition]:
    """一张图认完以后记成什么 engine、存什么字，顺带记报告里的计数。在主线程里跑；鉴权失败抛出去整批停。"""
    if future is None:
        return f"{engine}:skipped", Recognition("")
    result, exc, _seconds = future.result()
    if exc is not None:
        if isinstance(exc, OcrImageSkipped):
            logger.info("跳过图片 %s：%s", path, exc)
            return f"{engine}:skipped", Recognition("")
        if _fatal_ocr(exc):
            raise exc
        # 单张图坏了不拖垮整批
        logger.warning("图片识别失败 %s：%s", path, exc)
        report["failed"] += 1
        report["last_error"] = str(exc)[:300]
        return f"{engine}:error", Recognition("")
    used = engine
    if result.error:
        logger.warning("图片识别结果读不出来 %s：%s", path, result.error)
        used = f"{engine}:invalid"
        report["invalid"] += 1
        report["last_error"] = result.error[:300]
        result = Recognition("", None, result.usage)
    elif result.units is not None:
        report["structured"] += 1
    else:
        report["plain"] += 1
    if result.usage:
        report["metered"] += 1
        for name, value in result.usage.items():
            report["tokens"][name] = report["tokens"].get(name, 0) + value
    return used, result


def run_ocr(
    conn,
    data_dir: Path,
    limit: int,
    recognize: Callable[[Path], Recognition | str],
    engine: str | None = None,
    deadline: float | None = None,
    *,
    redo: bool = False,
    article_ids: list[int] | None = None,
    workers: int = 1,
) -> dict:
    """识别一批图片，重切引用它们的文章的图片文字块。返回计数。

    单张失败记 ``{engine}:error`` 和空正文，不写入脏字。没凭证或 401/403 整批停。
    结构读不出来记 ``{engine}:invalid``，不自动重试。
    engine 是写进 image_texts 的名字，不给就用默认的 deepseek-flash-vision。
    recognize 交回 ``Recognition``；交回字符串的当旧格式（测试和以前的引擎这样写）。
    报告里 structured 是按结构交回的张数，plain 是交回自由文字的，tokens 是接口报的用量合计。
    不点名文章时，自动上线在等（``health.deploy_waiting``）就认完手上这张停下，deploy_waiting 记它等的提交：
    以前长跑的识图会把之后的合并一直挡在外面。停下的从没认完的接着认，不白花钱。
    workers 张图一起认（模型调用在工作线程里，写库都在这个线程里，按挑出来的先后写）。
    全库重认（redo 且不点名文章）改了上千篇的图片文字：短帖的摘要和观点照常按发布时间排进夜间的队，
    不记进 article_followups 插到新文章前面，也不在这里当场重写。
    """
    from researchlib.health import deploy_waiting

    engine = engine or vision_engine_id()
    report = {
        "images": 0,
        "with_text": 0,
        "failed": 0,
        "invalid": 0,
        "structured": 0,
        "plain": 0,
        "missing": 0,
        "articles": 0,
        "tokens": {},
        "metered": 0,
        "last_error": None,
        "deploy_waiting": "",
        "followups": {"digest": [], "zsxq_title": []},
        "ocr_changed": [],
    }
    touched: set[int] = set()
    rows = iter(pending_images(conn, limit, redo=redo, article_ids=article_ids))
    width = max(1, int(workers or 1))

    def stopping() -> bool:
        if deadline is not None and time.monotonic() > deadline:
            return True
        if not article_ids:
            report["deploy_waiting"] = deploy_waiting(data_dir)
            return bool(report["deploy_waiting"])
        return False

    try:
        with ThreadPoolExecutor(max_workers=width) as pool:
            queue: deque = deque()

            def fill() -> None:
                # 同时在认的不超过 width 张。到点了或自动上线在等就不再开新的，已经在认的认完照写，钱不白花。
                while len(queue) < width and not stopping():
                    row = next(rows, None)
                    if row is None:
                        return
                    path = Path(data_dir) / "images" / row["storage_path"]
                    if not path.is_file():
                        report["missing"] += 1
                        continue
                    if path.suffix.lower() not in OCR_EXTS:
                        queue.append((row, path, None))
                    else:
                        queue.append((row, path, pool.submit(recognize_one, recognize, path)))

            fill()
            while queue:
                row, path, future = queue.popleft()
                used, result = _outcome(engine, path, future, report)
                text = result.text or ""
                conn.execute(
                    """
                    INSERT INTO image_texts (content_hash, engine, text, units, usage, prompt_version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (content_hash) DO UPDATE
                        SET engine = EXCLUDED.engine, text = EXCLUDED.text, units = EXCLUDED.units,
                            usage = EXCLUDED.usage, prompt_version = EXCLUDED.prompt_version, created_at = now()
                    """,
                    (
                        row["content_hash"],
                        used,
                        text,
                        Jsonb(result.units) if result.units is not None else None,
                        Jsonb(result.usage) if result.usage else None,
                        VISION_PROMPT_VERSION,
                    ),
                )
                report["images"] += 1
                if text.strip():
                    report["with_text"] += 1
                _touch_articles(conn, row["content_hash"], text, touched, report)
                conn.commit()
                fill()
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
            report["ocr_changed"] = [int(row["id"]) for row in changed]
        if changed and not (redo and not article_ids):
            try:
                from researchlib.followups import note_ocr_changes

                report["followups"] = note_ocr_changes(conn, changed)
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
    parser.add_argument(
        "--redo",
        action="store_true",
        help="连已经识别过的图也再认一遍：点名文章的全部重认；不点名时只认按旧说明认的，停了再开接着认",
    )
    parser.add_argument("--article", action="append", type=int, default=[], help="只认这篇文章里的图，可重复")
    # 只剩 vision。留着这个参数，以前写了 --engine vision 的命令（scripts/nightly.sh）照样能跑。
    parser.add_argument("--engine", choices=["vision"], default="vision", help="站内 deepseek-flash 识图")
    parser.add_argument(
        "--compare",
        type=int,
        default=0,
        metavar="N",
        help="挑 N 张按旧说明认过的图用现在的说明重认，不写库，原图和新旧结果打成一个包（data/ocr-compare/）",
    )
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
    if args.compare > 0:
        return _compare(conn, settings, recognize, args.compare)
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
            workers=settings.llm_concurrency,
        )
        follow = report.get("followups") or {}
        n_digest = len(follow.get("digest") or [])
        n_title = len(follow.get("zsxq_title") or [])
        if n_digest or n_title:
            print(f"短正文图片文字变了：摘要重排 {n_digest} 篇，标题重排 {n_title} 篇")
            if report["deploy_waiting"]:
                # 让路就要快：排上的留在队里，夜间摘要先补它们。
                print("自动上线在等，先不重写，留在队里")
            else:
                from researchlib.followups import drain_ocr_followups

                drained = drain_ocr_followups(conn, settings, follow)
                print(f"已重写摘要 {drained['digest']} 篇，标题 {drained['titles']} 篇")
        if args.redo and not args.article and report["ocr_changed"]:
            print(f"图片文字变了的 {len(report['ocr_changed'])} 篇：短帖的摘要和观点按发布时间排进夜间的队")
        remaining = redo_remaining(conn) if args.redo and not args.article else None
    finally:
        conn.close()
    print(
        f"引擎 {engine_name}：识别 {report['images']} 张图，有字的 {report['with_text']} 张，"
        f"失败 {report['failed']} 张，文件缺失 {report['missing']} 张，"
        f"更新了 {report['articles']} 篇的图片文字"
    )
    for line in describe_report(report):
        print(line)
    if remaining is not None:
        print(f"按旧说明认的图还剩 {remaining['images']} 张，其中以前认出过字的 {remaining['with_text']} 张")
    if report["deploy_waiting"]:
        print(
            f"自动上线在等 {report['deploy_waiting'][:7]}：先停在这里，"
            "/health 报出新提交后再跑一次，从没认完的接着认"
        )
    return 0


def _compare(conn, settings, recognize, size: int) -> int:
    """--compare：新旧识图对比包，见 ``researchlib.ocr_compare``。不写库。"""
    from researchlib.ocr_compare import run_compare

    workers = max(1, int(settings.llm_concurrency or 1))
    try:
        report = run_compare(conn, settings.data_dir, recognize, size, workers=workers)
    finally:
        conn.close()
    bundle = Path(report["bundle"])
    print(
        f"对比包：{bundle}（仓库目录下的 data/{bundle.parent.name}/{bundle.name}）。{report['images']} 张图："
        f"按结构交回 {report['structured']} 张，自由文字 {report['plain']} 张，失败或读不出来 {report['failed']} 张"
    )
    tokens = report["tokens"]
    if report["images"]:
        print(
            f"用量：输入 {tokens.get('prompt', 0)} token，输出 {tokens.get('completion', 0)} token"
            f"（其中思考 {tokens.get('reasoning', 0)}）；平均每张 {report['seconds'] / report['images']:.0f} 秒，"
            f"{workers} 张一起认"
        )
    return 0


def describe_report(report: dict) -> list[str]:
    """识图报告里格式和用量那几行：按结构交回的比例、读不出来的张数、接口报的 token。"""
    lines = []
    answered = report.get("structured", 0) + report.get("plain", 0) + report.get("invalid", 0)
    if answered:
        line = (
            f"其中按结构交回 {report.get('structured', 0)} 张，"
            f"交回自由文字 {report.get('plain', 0)} 张（按旧规则整理）"
        )
        if report.get("invalid"):
            line += f"，结构读不出来 {report['invalid']} 张（记 :invalid，不自动重试；--redo --article 点名重认）"
        lines.append(line)
    tokens = report.get("tokens") or {}
    metered = report.get("metered", 0)
    if metered:
        prompt, completion = tokens.get("prompt", 0), tokens.get("completion", 0)
        thinking = f"（其中思考 {tokens['reasoning']}）" if "reasoning" in tokens else ""
        lines.append(
            f"用量（接口报了用量的 {metered} 张）：输入 {prompt} token，输出 {completion} token{thinking}；"
            f"平均每张输入 {round(prompt / metered)}、输出 {round(completion / metered)}"
        )
    elif answered:
        lines.append("接口这次没报用量，花费看 DeepSeek 账单")
    return lines


if __name__ == "__main__":
    sys.exit(main())

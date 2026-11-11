"""图片里的字进检索（OCR / 识图）。

截图里的财报表格、聊天记录、标注过的图表，检索和问库原来完全看不到。把本地图片交给
站内 LLM 识图（2026-11-10 起是 Grok OAuth 的 grok-4.7，``llm.chat``，多模态 content），
按内容哈希只认一次，结果存进 image_texts（engine 如 ``grok-4.7-vision``）。识别出的字够多的图，切成段落块进检索
（article_chunks.lang = 'ocr'，标题「图片里的字」，锚点指向文章页上那张图）。
公司提及默认不看图片里的字，免得水印、表情包乱挂；正文短于 SHORT_BODY_CHARS 时例外，
截图帖要挂上图里的公司才能抽分家观点。

识图交回结构（JSON，见 ``researchlib.ocr_units``）：谁说的、是不是截图主人、引用的哪条、哪条被图片边缘截断，
都由看得见图的模型标好，存进 image_texts.units，后面只排版和拼接，不再按规则猜。
模型偶尔交回的一段自由文字、全量重认以前存的旧识图（units 为空），都按空行当成几段文字（``ocr_units.plain_units``），
不再按 App、按案例的规则猜说话人、剥界面字（那套规则删了）。
长截图（比手机一屏长得多的）切成几段有重叠的图分别认，再由 ``ocr_units.join_tiles`` 接回一张（见 ``tile_spans``）；
``--tall`` 只重认切段以前整张认的长图（和 11-10 晚以前切段认、没存每段结果的）。

app 容器就能跑。单张识图失败记 ``engine`` 以 ``:error`` 结尾、正文为空，
下次不带 ``--redo`` 也会重试，不写进脏字。JSON 连着两次读不出来记 ``:invalid``，
不自动重试（重试又要花钱），``--redo --article`` 点名重认。重认（``--redo``）时这张图库里已经有字的，
失败或读不出来都不写，旧字留着、版本不动，下次 ``--redo`` 接着认。余额不足（402）和没凭证一样整批停。

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
import itertools
import logging
import math
import re
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
    join_tiles,
    parse_reply,
    plain_units,
    stitch,
    structure_of,
)
from researchlib.wechat_dialogue import HOST

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
# 长截图（一屏屏往下截、拼成的一张）整张送去认，长边压到 VISION_MAX_SIDE 以后宽只剩几百像素，字小到认错、改了意思
# （11-09 抽查：1080×7237 的图送出去宽 286，错 6 处）。DeepSeek 那边每张图也只看约 1300×1300 像素的量，送原图一样缩。
# 比 VISION_MAX_SIDE 高、又高过宽的 VISION_TALL_RATIO 倍的，切成几段分别认（见 tile_spans）：每段高不超过宽的
# VISION_TILE_RATIO 倍，和手机一屏差不多，压完的字和普通截图一样大；相邻两段重叠宽的 VISION_TILE_OVERLAP 倍，
# 认完由 ocr_units.join_tiles 接回一张。手机一屏的高宽比约 2.2，这个比例以下的都不切。
VISION_TALL_RATIO = 3
VISION_TILE_RATIO = 2
VISION_TILE_OVERLAP = 0.4
# 一张图最多切这么多段（高是宽的 20 倍左右）。再长的每段跟着变高，一张图花的钱有个头。
VISION_MAX_TILES = 12
# --tall 结尾列出这次重认的长图挂在哪几篇上，最多列这么多篇。
TALL_SHOW_ARTICLES = 30
# 找切口时，一行的「花」（``_row_noise``）不超过附近最空那行加这么多，就算空白。JPEG 的底色上有一两个灰度的噪点，
# 压着字的行一般在 10 以上。
VISION_QUIET_SLACK = 2.0
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
  被图片上下边缘截掉一部分（这条在图外面还有没截进来的字，看得见的字是完整的）加 "cut": "top" 或 "bottom"。\
cut 只表示这句话在图外还有下文：看得见的字整段照抄，只写看得到的字，不要补全，也不要因为标了 cut 就删掉。\
字本身被切到笔画中间、只看得到上半截或下半截、一个也认不全的，加 "illegible": true，text 写空串，这条不写进正文；\
不要把看不全的笔画补成一句通顺的话。看得清的整行即使这条在图外还有下文，也不要标 illegible。
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
# 11-10 一度把所有 cut 都换成「无法辨认」，把延伸到图外、字却看得清的整段丢掉了。
# 改回：cut 留字；只有 illegible（笔画被切到中间）才不采用模型补的那句。
# 版本仍是 3：按第 3 版认过的图（包括没有 illegible 字段的旧行）不再重认，排版也和改之前一样留字。
VISION_PROMPT_VERSION = 3


class OcrUnavailable(Exception):
    pass


class OcrImageSkipped(Exception):
    """这张图不再试（太大）。run_ocr 记 :skipped，不占重试。"""


def vision_engine_id(settings=None) -> str:
    """写入 image_texts.engine 的名字，跟着当前模型走：grok-4.7-vision。

    11-10 换成 Grok 以后新的识图记 ``grok-4.7-vision``；以前按 deepseek-flash 认的行原样留着，
    重认只看 prompt_version（``pending_images``），换模型不会把按第 3 版说明认过的图再认一遍。
    """
    from researchlib.grok import GROK_MODEL_ID
    from researchlib.llm_provider import PROVIDER_GROK, llm_provider

    model = GROK_MODEL_ID if llm_provider() == PROVIDER_GROK else "deepseek-flash"
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
        import PIL  # noqa: F401
    except ImportError as exc:
        if len(image_bytes) > MAX_VISION_SEND_BYTES:
            raise OcrImageSkipped(
                f"超过 {MAX_VISION_SEND_BYTES} 字节且未装 Pillow，识图跳过"
            ) from exc
        return image_bytes, mime

    return _shrink_to_jpeg(_open_rgb(image_bytes)), "image/jpeg"


def _open_rgb(image_bytes: bytes):
    """解码成 RGB 的 Pillow 图。打不开的当跳过。"""
    from io import BytesIO

    from PIL import Image

    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
    except Exception as exc:  # noqa: BLE001 — 坏图当跳过
        raise OcrImageSkipped(f"图片打不开：{exc}") from exc
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def _shrink_to_jpeg(image) -> bytes:
    """长边缩到 VISION_MAX_SIDE 以内，存成 JPEG；还超过 MAX_VISION_SEND_BYTES 就再缩、降质量，最多 8 次。"""
    from io import BytesIO

    from PIL import Image

    width, height = image.size
    longest = max(width, height) or 1
    scale = min(1.0, VISION_MAX_SIDE / longest)
    if scale < 1.0:
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
    quality = VISION_JPEG_QUALITY
    for _ in range(8):
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= MAX_VISION_SEND_BYTES:
            return out
        width, height = image.size
        image = image.resize(
            (max(1, int(width * 0.85)), max(1, int(height * 0.85))),
            Image.Resampling.LANCZOS,
        )
        quality = max(40, quality - 5)
    raise OcrImageSkipped(f"压缩后仍超过 {MAX_VISION_SEND_BYTES} 字节，识图跳过")


def needs_tiles(width: int, height: int) -> bool:
    """长截图：比 VISION_MAX_SIDE 高，又高过宽的 VISION_TALL_RATIO 倍。整张压完字太小，要切段认。"""
    return height > VISION_MAX_SIDE and height > VISION_TALL_RATIO * width


def tile_count(width: int, height: int) -> int:
    """要切成几段：每段不高过宽的 VISION_TILE_RATIO 倍，最多 VISION_MAX_TILES 段。不用切的是 1。"""
    if not needs_tiles(width, height):
        return 1
    overlap = VISION_TILE_OVERLAP * width
    tile = VISION_TILE_RATIO * width
    return min(VISION_MAX_TILES, max(2, math.ceil((height - overlap) / (tile - overlap))))


def tile_spans(
    width: int, height: int, noise: Callable[[int, int], list[float]] | None = None
) -> list[tuple[int, int]]:
    """长截图切成的几段，从上往下每段的 (上边, 下边)。不用切的是 [(0, height)]。

    先按段数等分：相邻两段重叠宽的 VISION_TILE_OVERLAP 倍。每个切口再挪到附近（重叠的四分之一以内）
    最空的一条横带正中：一连串够空的行算一条横带，挑最宽的，一样宽挑离原位近的。行与行、两条消息之间的空白
    都是这样的横带，切在那里，挨着切口的字是整行的；两条切口各挪不出四分之一，两段至少还重叠一半。
    noise(上, 下) 交回这几行各自有多「花」（``_row_noise``）；不给就不挪，只算位置。
    """
    count = tile_count(width, height)
    if count == 1:
        return [(0, height)]
    overlap = VISION_TILE_OVERLAP * width
    step = (height - overlap) / count
    reach = overlap / 4

    def cut(centre: float) -> int:
        if noise is None:
            return round(centre)
        return _quiet_row(noise, centre, reach, height)

    spans = []
    top = 0
    for index in range(1, count):
        start = index * step  # 下一段的上边；这一段的下边在 start + overlap
        spans.append((top, cut(start + overlap)))
        top = cut(start)
    spans.append((top, height))
    return spans


def _quiet_row(noise: Callable[[int, int], list[float]], centre: float, reach: float, height: int) -> int:
    """centre 上下 reach 行以内最空的一条横带的正中那一行。"""
    low = max(1, int(centre - reach))
    high = min(height - 1, int(centre + reach) + 1)
    if high <= low:
        return min(max(1, round(centre)), height - 1)
    scores = noise(low, high)
    floor = min(scores)
    best_key: tuple[int, float] | None = None
    best = round(centre)
    start: int | None = None
    for offset, score in enumerate([*scores, math.inf]):
        if score <= floor + VISION_QUIET_SLACK:
            if start is None:
                start = offset
            continue
        if start is not None:
            middle = low + (start + offset - 1) // 2
            key = (offset - start, -abs(middle - centre))
            if best_key is None or key > best_key:
                best_key, best = key, middle
            start = None
    return best


def _row_noise(image) -> Callable[[int, int], list[float]]:
    """每一行左右相邻两个像素灰度差的平均：整行是底色是 0，穿过气泡、深色配图的空白只有几条边，
    压着字的行笔画多，大得多。

    不用整行的标准差：深色配图左右是白边，图里没字的那几行标准差也很大，切口就挑不到图里的空白。
    """
    from PIL import Image, ImageChops

    gray = image.convert("L")
    width = gray.width

    def noise(top: int, bottom: int) -> list[float]:
        if width < 2:
            return [0.0] * (bottom - top)
        band = gray.crop((0, top, width, bottom))
        steps = ImageChops.difference(band.crop((1, 0, width, band.height)), band.crop((0, 0, width - 1, band.height)))
        means = steps.convert("F").resize((1, band.height), Image.Resampling.BOX)
        return [float(means.getpixel((0, row))) for row in range(band.height)]

    return noise


def image_size(path: Path) -> tuple[int, int] | None:
    """图的宽高，只读文件头。读不出来是 None。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:  # noqa: BLE001 — 坏图、没装 Pillow 都当不知道
        return None


def prepare_vision_images(image_bytes: bytes, mime: str) -> list[tuple[bytes, str]]:
    """送识图的图 [(bytes, mime)]：普通的图一张（``prepare_vision_image``），长截图按 ``tile_spans`` 切成几段各自压。"""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as probe:
            width, height = probe.size
    except Exception:  # noqa: BLE001 — 读不出宽高的照旧整张送，坏图由 prepare_vision_image 记跳过
        return [prepare_vision_image(image_bytes, mime)]
    if not needs_tiles(width, height):
        return [prepare_vision_image(image_bytes, mime)]
    image = _open_rgb(image_bytes)
    return [
        (_shrink_to_jpeg(image.crop((0, top, image.width, bottom))), "image/jpeg")
        for top, bottom in tile_spans(image.width, image.height, _row_noise(image))
    ]


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


@dataclass
class Recognition:
    """一张图识出来的东西。认成了 units 就有（模型没按结构交的自由文字也当成几段文字），error 非空是结构读不出来。"""

    text: str
    units: dict | None = None
    usage: dict = field(default_factory=dict)
    error: str = ""


# 模型回了 JSON 却读不出来时，同一张图最多再要这么多次。还不行记 :invalid，不再自动重试，免得每晚为它花钱。
VISION_UNITS_RETRIES = 1


def read_vision_reply(reply: str) -> tuple[str, dict]:
    """模型的回答 → (存进 image_texts.text 的字, units)。

    按结构交的：字由 units 排出来（读旧列的地方照样能用），units 存原样结构。
    没按结构交的（自由文字）：按空行当成几段文字（``plain_units``），字照原样留着，不按规则猜说话人、剥界面字。
    像 JSON 却读不出来的抛 UnitsError。
    """
    units = parse_reply(reply)
    if units is None:
        units = plain_units(reply)
    return format_units(units), units


def vision_engine(settings) -> Callable[[Path], Recognition]:
    """站内识图。失败抛给 run_ocr，记成 :error。

    先压图再送模型，避免转发链路把大 data-url 掐断；瞬时网络错误最多再试两次。
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

    def read(path: Path, payload: bytes, mime: str, usage: dict) -> tuple[str, dict] | str:
        """认一张（或长图的一段）：交回 (字, units)；读不出来交回出错的原因。"""
        messages = vision_messages(payload, mime)
        problem = ""
        for _attempt in range(1 + VISION_UNITS_RETRIES):
            try:
                return read_vision_reply(ask(messages, usage))
            except UnitsError as exc:
                problem = str(exc)
                logger.info("识图结构读不出来 %s：%s", path, exc)
        return problem or "识图结构读不出来"

    def recognize(path: Path) -> Recognition:
        raw = path.read_bytes()
        if len(raw) > MAX_VISION_BYTES:
            raise OcrImageSkipped(f"超过 {MAX_VISION_BYTES} 字节，识图跳过")
        pieces = prepare_vision_images(raw, image_mime(path))
        if len(pieces) > 1:
            logger.info("长截图切成 %d 段认 %s", len(pieces), path)
        usage: dict = {}
        found: list[tuple[str, dict]] = []
        for payload, mime in pieces:
            # 一段出错或读不出来，整张都不算认成：只写一部分的字比旧字还糟，重认时旧字留着。
            result = read(path, payload, mime, usage)
            if isinstance(result, str):
                return Recognition("", None, usage, result)
            found.append(result)
        if len(found) == 1:
            text, units = found[0]
            return Recognition(text, units, usage)
        units = join_tiles([units for _text, units in found])
        return Recognition(format_units(units), units, usage)

    return recognize


def resolve_engine(settings) -> tuple[str, Callable[[Path], Recognition]]:
    """返回 (写入 image_texts 的 engine 名, recognize)。没有凭证就起不来。

    单张 HTTP 失败不在这里处理，由 run_ocr 记 :error。
    """
    from researchlib.llm import llm_configured

    if not llm_configured(settings):
        from researchlib.llm_provider import PROVIDER_GROK, llm_provider

        provider = getattr(settings, "llm_provider", None) or llm_provider()
        if provider == PROVIDER_GROK:
            raise OcrUnavailable("还没登录 Grok OAuth，识图起不来。先运行 research-xai-login login。")
        raise OcrUnavailable("没有配置 DEEPSEEK_API_KEY，识图起不来。")
    return vision_engine_id(settings), vision_engine(settings)


def _fatal_ocr(exc: Exception) -> bool:
    """整批停：没凭证，接口拒绝鉴权，或余额不足（402）。单张 5xx 不算。

    余额用完以后每张都马上失败，接着认只会把剩下的图一张张记成失败。
    """
    from researchlib.llm import LLMUnavailable

    if isinstance(exc, (OcrUnavailable, LLMUnavailable)):
        return True
    text = str(exc)
    return any(f"接口返回 {code}" in text for code in (401, 402, 403))


def _has_text(conn, content_hash: str) -> bool:
    """库里这张图已经有认出来的字。重认没成时留着它，不拿空正文盖掉。"""
    row = conn.execute(
        "SELECT 1 FROM image_texts WHERE content_hash = %s AND btrim(coalesce(text, '')) <> ''",
        (content_hash,),
    ).fetchone()
    return row is not None


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


def _tall_candidates(conn) -> list:
    """按现在的说明认过、没存每段结果的图（``units`` 里没有 parts），从新到旧。宽高要读文件才知道，由调用方筛。

    没有 parts 的长图是整张认的，或者 11-10 晚以前切段认的（那时接缝会把重叠的一截写两遍，也没存每段结果，
    接法改了重接不了）。按旧说明认的不在这里：全库重认（不点名文章的 --redo）认它们时，长图已经切段。
    结果读不出来（:invalid）、失败（:error）的也算，切段以后常常就认得出来；:skipped（太大、不是图）的不算。
    """
    where = [
        "a.duplicate_of IS NULL",
        site_visible_sql("a"),
        "EXISTS (SELECT 1 FROM sources s WHERE s.id = a.source_id AND s.enabled)",
        "EXISTS (SELECT 1 FROM image_texts t WHERE t.content_hash = i.content_hash"
        " AND t.prompt_version >= %s AND right(t.engine, 8) <> ':skipped' AND t.units -> 'parts' IS NULL)",
    ]
    return conn.execute(
        f"""
        SELECT i.content_hash, min(i.storage_path) AS storage_path
        FROM article_images i
        JOIN articles a ON a.id = i.article_id
        WHERE {" AND ".join(where)}
        GROUP BY i.content_hash
        ORDER BY max(a.published_at) DESC NULLS LAST
        """,
        (VISION_PROMPT_VERSION,),
    ).fetchall()


def _tall_size(data_dir: Path, row) -> tuple[int, int] | None:
    size = image_size(Path(data_dir) / "images" / row["storage_path"])
    return size if size and needs_tiles(*size) else None


def pending_tall_images(conn, data_dir: Path, limit: int) -> list:
    """还要切段重认的长截图（``needs_tiles``，见 ``_tall_candidates``），从新到旧，最多 limit 张。
    ``research-ocr --tall`` 切段重认它们。

    长图切段以前，整张压到长边 VISION_MAX_SIDE，宽只剩几百像素，认出来的字错得多。
    库里不记图的宽高：挑出按现在的说明认过、没切段的图，逐张读文件头（几十字节）筛出长的。文件读不出来的不算。
    """
    found = []
    for row in _tall_candidates(conn):
        if len(found) >= limit:
            break
        if _tall_size(data_dir, row):
            found.append(row)
    return found


def tall_remaining(conn, data_dir: Path) -> dict:
    """--tall 还要认多少张长图、切成多少段（每段的花费和一张普通截图差不多）。"""
    images = tiles = 0
    for row in _tall_candidates(conn):
        size = _tall_size(data_dir, row)
        if size:
            images += 1
            tiles += tile_count(*size)
    return {"images": images, "tiles": tiles}


def useful_image_text(text: str) -> bool:
    """汉字、字母、数字够 READING_MIN_CHARS 个，才算图里有可读的字。

    空白、水印、几个字的装饰不算。刚过线的拿不准，算有用，不能漏。
    不按「是不是微信、是不是表格」分：有可读的字就留。
    """
    return len(_MEANINGFUL.findall(text or "")) >= READING_MIN_CHARS


def compose_ocr_text(pieces: list, owners: tuple[str, ...] = ()) -> str:
    """按图片顺序拼成观点和摘要能读的旁路正文。

    pieces 是每张图的识别结果：字符串（没有结构的字），或 ``ImageText``。
    owners 是这篇来源作者本人的名字（``article_owners``）：图里显示名是这些账号的，写成作者。
    太短的（水印、表情）丢掉。这份字给摘要、观点、问库和检索；阅读页不铺进正文，只留原图。
    每张图只排版、按首尾重合拼接（``ocr_units``）；没有结构的（units 为空的旧识图）当成几段文字照原样排。
    """
    records = [piece if isinstance(piece, ImageText) else ImageText("", piece or "", None) for piece in pieces]
    return "\n\n".join(text for _record, text in _image_pieces(records, owners) if text)


def _image_pieces(records: list[ImageText], owners: tuple[str, ...] = ()) -> list[tuple[ImageText, str]]:
    """一篇的图：[(那张图, 排好的字)]。同一张图出现两次只算第一次。

    挨着的两张按首尾重合拼接后排版，引用的那条前面写过就不再写。字太少的那张是空串。
    """
    seen_digests: set[str] = set()
    ordered: list[ImageText] = []
    for record in records:
        if record.digest and record.digest in seen_digests:
            continue
        if record.digest:
            seen_digests.add(record.digest)
        ordered.append(record)
    found: list[tuple[ImageText, str]] = []
    written: set[str] = set()
    for record, blocks in zip(ordered, stitch([structure_of(record) for record in ordered]), strict=True):
        text = format_blocks(blocks or [], written, owners)
        found.append((record, text if useful_image_text(text) else ""))
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
    """摘要和观点读的图片文字：(articles.ocr_text, ocr_hash)。

    这篇还挂着 image_texts 时，先按现在的排版从识别结果重写 articles.ocr_text，重写出来的就是定稿。
    没有图的照库里的原样交。
    """
    if article_image_texts(conn, article_id):
        refresh_article_ocr(conn, article_id)
    row = conn.execute(
        "SELECT ocr_text, ocr_hash FROM articles WHERE id = %s",
        (article_id,),
    ).fetchone()
    if row is None:
        return "", ""
    return row["ocr_text"] or "", row["ocr_hash"] or ""


def speaker_note(host: str = HOST) -> str:
    """图片里有对话时，摘要和观点的提示里补的那句说话人说明。host 是这篇的作者（文字已经 relabel_host 过）。"""
    name = (host or "").strip() or HOST
    if name == HOST:
        return (
            "截图里的对话已整理成「说话人：正文」：绿气泡是星主方伟，其余说话人写的是截图上的网名。"
            "方伟是星主，别人的话不要写成方伟的判断。"
        )
    return (
        f"截图里的对话已整理成「说话人：正文」：绿气泡是这篇的作者{name}，其余说话人写的是截图上的网名。"
        f"{name}是发这篇的作者，别人的话不要写成{name}的判断。"
    )


def ocr_speaker_note(conn, article_id: int, text: str, host: str) -> str:
    """摘要和观点提示里那句说话人说明（``speaker_note``）。text 是已经换成作者名的图片文字。

    谁说的话是识图标好的：有一张图里有消息就加这句，没有就不加。没有结构的字（旧识图）不按冒号前的字猜。
    """
    if not (text or "").strip():
        return ""
    if not any(has_dialogue(image.units) for image in article_image_texts(conn, article_id)):
        return ""
    return speaker_note(host)


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
        if result.units.get("tiles"):
            report["tiled"] = report.get("tiled", 0) + 1
            report["tiles"] = report.get("tiles", 0) + int(result.units["tiles"])
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
    tall: bool = False,
) -> dict:
    """识别一批图片，重切引用它们的文章的图片文字块。返回计数。

    单张失败记 ``{engine}:error`` 和空正文，不写入脏字。没凭证、401/403 或余额不足（402）整批停。
    结构读不出来记 ``{engine}:invalid``，不自动重试。
    redo 时这张图库里已经有字的，失败或结构读不出来都不写：旧字留着，版本不动，下次 --redo 接着认，
    报告里记 kept。以前写成空正文，重认一失败旧字就没了。
    engine 是写进 image_texts 的名字，不给就按当前模型算（Grok 是 grok-4.7-vision）。
    recognize 交回 ``Recognition``；交回字符串的当旧格式（测试和以前的引擎这样写）。
    报告里 structured 是按结构交回的张数，plain 是交回自由文字的，tokens 是接口报的用量合计。
    不点名文章时，自动上线在等（``health.deploy_waiting``）就认完手上这张停下，deploy_waiting 记它等的提交：
    以前长跑的识图会把之后的合并一直挡在外面。停下的从没认完的接着认，不白花钱。
    workers 张图一起认：模型调用在工作线程里，写库都在这个线程里，认完一张写一张、马上补发一张。
    以前按挑出来的先后写，前面一张慢（思考长的要好几分钟）就把后面都挡住，认完的占着位子不补发，
    20 张一起实际只有一半左右在认。
    全库重认（redo 且不点名文章）改了上千篇的图片文字：短帖的摘要和观点照常按发布时间排进夜间的队，
    不记进 article_followups 插到新文章前面，也不在这里当场重写。
    tall：只认还要切段重认的长截图（``pending_tall_images``）；和 redo 一起用，失败留着旧字。
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
        "kept": 0,
        "deploy_waiting": "",
        "followups": {"digest": [], "zsxq_title": []},
        "ocr_changed": [],
        "touched": [],
    }
    touched: set[int] = set()
    if tall:
        rows = iter(pending_tall_images(conn, data_dir, limit))
    else:
        rows = iter(pending_images(conn, limit, redo=redo, article_ids=article_ids))
    width = max(1, int(workers or 1))

    def stopping() -> bool:
        if deadline is not None and time.monotonic() > deadline:
            return True
        if not article_ids:
            report["deploy_waiting"] = deploy_waiting(data_dir)
            return bool(report["deploy_waiting"])
        return False

    def write(row, path: Path, future) -> None:
        used, result = _outcome(engine, path, future, report)
        report["images"] += 1
        text = result.text or ""
        if redo and used.endswith((":error", ":invalid")) and _has_text(conn, row["content_hash"]):
            report["kept"] += 1
            return
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
        if text.strip():
            report["with_text"] += 1
        _touch_articles(conn, row["content_hash"], text, touched, report)
        conn.commit()

    try:
        with ThreadPoolExecutor(max_workers=width) as pool:
            running: dict = {}  # 在认的：future -> (挑出来的序号, row, path)
            order = itertools.count()

            def fill() -> None:
                # 同时在认的不超过 width 张。到点了或自动上线在等就不再开新的，已经在认的认完照写，钱不白花。
                while len(running) < width and not stopping():
                    row = next(rows, None)
                    if row is None:
                        return
                    path = Path(data_dir) / "images" / row["storage_path"]
                    if not path.is_file():
                        report["missing"] += 1
                        continue
                    if path.suffix.lower() not in OCR_EXTS:
                        write(row, path, None)
                        continue
                    running[pool.submit(recognize_one, recognize, path)] = (next(order), row, path)

            fill()
            while running:
                done, _pending = wait(running, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: running[item][0]):
                    _index, row, path = running.pop(future)
                    write(row, path, future)
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
        report["touched"] = [int(article_id) for article_id in sorted(touched)]
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


def parse_written_at(value: str):
    """``--written-from`` / ``--written-to``。没带时区按北京时间（HKT 同一时刻）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    text = (value or "").strip()
    if not text:
        raise ValueError("时间是空的")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if "T" in text or "+" in text[1:] or text.count("-") > 2:
            moment = datetime.fromisoformat(text)
        else:
            moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"时间认不出来：{value}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return moment


def relayout_until(written_to):
    """上界变成不含的那一刻。写到秒就含这一整秒，03:33:02 含 03:33:02.350005，不含 03:33:03。

    写到微秒就只含到那一微秒。
    """
    from datetime import timedelta

    if written_to.microsecond == 0:
        return written_to + timedelta(seconds=1)
    return written_to + timedelta(microseconds=1)


def relayout_stored(
    conn,
    *,
    engine: str,
    written_from,
    written_to,
    dry_run: bool = False,
) -> dict:
    """按库里的 units 重排 ``image_texts.text`` 和引用这些图的文章旁路、检索块。不调模型。

    只动 ``engine`` 对得上、``created_at`` 落在这段里、并且有 units 的行。
    上界写到秒时含这一整秒（``created_at < 下一秒``），不会把 03:33:02.350005 漏在 03:33:02 外面。
    ``prompt_version`` 和 ``created_at`` 不动，所以全库 ``--redo`` 不会把它们再认一遍。
    """
    from researchlib.chunks import replace_ocr_chunks
    from researchlib.ocr_units import format_units

    rows = conn.execute(
        """
        SELECT content_hash, text, units
        FROM image_texts
        WHERE engine = %s AND units IS NOT NULL
          AND created_at >= %s AND created_at < %s
        ORDER BY created_at, content_hash
        """,
        (engine, written_from, relayout_until(written_to)),
    ).fetchall()
    changed: list[str] = []
    for row in rows:
        text = format_units(row["units"])
        if text == (row["text"] or ""):
            continue
        changed.append(row["content_hash"])
        if dry_run:
            continue
        conn.execute(
            "UPDATE image_texts SET text = %s WHERE content_hash = %s",
            (text, row["content_hash"]),
        )
    articles: list[int] = []
    if changed and not dry_run:
        found = conn.execute(
            "SELECT DISTINCT article_id FROM article_images WHERE content_hash = ANY(%s) ORDER BY article_id",
            (changed,),
        ).fetchall()
        articles = [int(item["article_id"]) for item in found]
        for article_id in articles:
            refresh_article_ocr(conn, article_id)
            replace_ocr_chunks(conn, article_id)
        conn.commit()
    return {"matched": len(rows), "changed": len(changed), "articles": len(articles), "dry_run": dry_run}


def rejoin_stored(conn, *, dry_run: bool = False) -> dict:
    """切段认的长截图按库里存的每段结果（``units.parts``）重新接一遍，重排 ``image_texts.text`` 和引用这些图的
    文章旁路、检索块。不调模型。接缝的接法（``ocr_units.join_tiles``）改了以后跑。

    ``engine``、``prompt_version``、``created_at`` 不动。11-10 以前切段认的图没存每段结果，不在这里面。
    """
    from researchlib.chunks import replace_ocr_chunks

    rows = conn.execute(
        """
        SELECT content_hash, text, units
        FROM image_texts
        WHERE units -> 'parts' IS NOT NULL
        ORDER BY created_at, content_hash
        """
    ).fetchall()
    changed: list[str] = []
    for row in rows:
        units = join_tiles(row["units"]["parts"])
        text = format_units(units)
        if units == row["units"] and text == (row["text"] or ""):
            continue
        changed.append(row["content_hash"])
        if dry_run:
            continue
        conn.execute(
            "UPDATE image_texts SET units = %s, text = %s WHERE content_hash = %s",
            (Jsonb(units), text, row["content_hash"]),
        )
    articles: list[int] = []
    if changed and not dry_run:
        found = conn.execute(
            "SELECT DISTINCT article_id FROM article_images WHERE content_hash = ANY(%s) ORDER BY article_id",
            (changed,),
        ).fetchall()
        articles = [int(item["article_id"]) for item in found]
        for article_id in articles:
            refresh_article_ocr(conn, article_id)
            replace_ocr_chunks(conn, article_id)
        conn.commit()
    return {"matched": len(rows), "changed": len(changed), "articles": len(articles), "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了，2 识图起不来（Grok 没登录，或 DeepSeek 没配 key）。"""
    parser = argparse.ArgumentParser(description="识别图片里的字，切成段落块进检索")
    parser.add_argument("--limit", type=int, default=500, help="这次最多识别多少张图")
    parser.add_argument("--max-minutes", type=float, default=0, help="跑到这么多分钟就停，0 表示不限")
    parser.add_argument(
        "--redo",
        action="store_true",
        help="连已经识别过的图也再认一遍：点名文章的全部重认；不点名时只认按旧说明认的，停了再开接着认",
    )
    parser.add_argument("--article", action="append", type=int, default=[], help="只认这篇文章里的图，可重复")
    parser.add_argument(
        "--tall",
        action="store_true",
        help="只重认以前整张认的（和 11-10 晚以前切段认、没存每段结果的）长截图，切段认；失败留着旧字。"
        "--limit 0 只报还剩多少张、要切多少段",
    )
    # 只剩 vision。留着这个参数，以前写了 --engine vision 的命令（scripts/nightly.sh）照样能跑。
    parser.add_argument("--engine", choices=["vision"], default="vision", help="站内 LLM 识图（grok-4.7）")
    parser.add_argument(
        "--compare",
        type=int,
        default=0,
        metavar="N",
        help="挑 N 张按旧说明认过的图用现在的说明重认，不写库，原图和新旧结果打成一个包（data/ocr-compare/）",
    )
    parser.add_argument(
        "--relayout",
        action="store_true",
        help="按库里的 units 重排文字，不调模型。要和 --relayout-engine、--written-from、--written-to 一起用",
    )
    parser.add_argument("--relayout-engine", default="", help="只重排 image_texts.engine 等于这个的行")
    parser.add_argument(
        "--written-from",
        default="",
        help="只重排 created_at 不早于这时的行。没写时区按北京时间，例如 2026-11-10 03:28:41",
    )
    parser.add_argument(
        "--written-to",
        default="",
        help="只重排 created_at 不晚于这时的行。写到秒就含这一整秒，03:33:02 含 03:33:02.350005",
    )
    parser.add_argument(
        "--rejoin",
        action="store_true",
        help="切段认的长截图按库里存的每段结果重新接一遍，不调模型。接缝的接法改了以后跑",
    )
    parser.add_argument("--dry-run", action="store_true", help="和 --relayout、--rejoin 一起用：只报张数，不写库")
    args = parser.parse_args(argv)
    if args.tall and (args.article or args.compare):
        parser.error("--tall 不和 --article、--compare 一起用：点名文章的 --redo --article 已经全部重认，长图自动切段")
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    if args.rejoin:
        if args.relayout or args.redo or args.tall or args.compare or args.article:
            print("--rejoin 不和 --relayout、--redo、--tall、--compare、--article 一起用", file=sys.stderr)
            return 2
        conn = prepare(settings)
        try:
            report = rejoin_stored(conn, dry_run=args.dry_run)
        finally:
            conn.close()
        verb = "会重接" if args.dry_run else "重接了"
        print(
            f"{verb} {report['changed']} 张（存了每段结果的长截图 {report['matched']} 张），"
            f"更新了 {report['articles']} 篇的图片文字。没有调模型。"
        )
        return 0
    if args.relayout:
        if args.redo or args.tall or args.compare or args.article:
            print("--relayout 不和 --redo、--tall、--compare、--article 一起用", file=sys.stderr)
            return 2
        if not args.relayout_engine or not args.written_from or not args.written_to:
            print("要写 --relayout-engine、--written-from、--written-to", file=sys.stderr)
            return 2
        try:
            written_from = parse_written_at(args.written_from)
            written_to = parse_written_at(args.written_to)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        conn = prepare(settings)
        try:
            report = relayout_stored(
                conn,
                engine=args.relayout_engine,
                written_from=written_from,
                written_to=written_to,
                dry_run=args.dry_run,
            )
        finally:
            conn.close()
        verb = "会重排" if args.dry_run else "重排了"
        print(
            f"{verb} {report['changed']} 张（范围内 {report['matched']} 张有结构），"
            f"更新了 {report['articles']} 篇的图片文字。没有调模型。"
        )
        return 0
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
            redo=args.redo or args.tall,
            article_ids=args.article or None,
            workers=settings.llm_concurrency,
            tall=args.tall,
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
        if (args.redo or args.tall) and not args.article and report["ocr_changed"]:
            print(f"图片文字变了的 {len(report['ocr_changed'])} 篇：短帖的摘要和观点按发布时间排进夜间的队")
        remaining = redo_remaining(conn) if args.redo and not args.article and not args.tall else None
        tall_left = tall_remaining(conn, settings.data_dir) if args.tall else None
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
    if tall_left is not None:
        print(
            f"还要切段重认的长截图还剩 {tall_left['images']} 张，切段要认 {tall_left['tiles']} 段"
            "（每段的花费和一张普通截图差不多）"
        )
        if report["touched"]:
            # 对着原图核接缝时要知道去看哪几篇：/api/v1/articles/{id} 里 lang=ocr 的块。
            shown = "、".join(str(article_id) for article_id in report["touched"][:TALL_SHOW_ARTICLES])
            more = f" 等 {len(report['touched'])} 篇" if len(report["touched"]) > TALL_SHOW_ARTICLES else ""
            print(f"这次切段重认的长图在这几篇：{shown}{more}")
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
            line += f"，结构读不出来 {report['invalid']} 张（记成 :invalid 的不自动重试，--redo --article 点名重认）"
        lines.append(line)
    if report.get("tiled"):
        lines.append(f"长截图 {report['tiled']} 张切成 {report['tiles']} 段认（每段的花费和一张普通截图差不多）")
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
        lines.append("接口这次没报用量，花费看提供方账单")
    if report.get("kept"):
        lines.append(f"重认没认成的 {report['kept']} 张留着旧字，版本没改，下次 --redo 接着认")
    return lines


if __name__ == "__main__":
    sys.exit(main())

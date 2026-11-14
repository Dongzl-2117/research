"""识图的结构化结果：模型按 JSON 交回图里有什么，这里只校验、排版，把连着截的几张拼起来。

以前识图交回一段自由文字，后面用几百行按 App、按案例写的正则去猜：哪行是谁说的、哪行是界面、
哪几句和上一张截图重复。这些在图上一眼可见，写成文字以后就丢了，所以每来一种新版式就要补一条规则。
现在由看得见图的模型直接交结构（提示词是 ``researchlib.ocr.VISION_PROMPT``）::

    {"kind": "chat",
     "blocks": [
       {"type": "message", "speaker": "王小明", "self": false, "text": "毛利率：35%，比去年高", "cut": "top"},
       {"type": "message", "speaker": "", "self": true, "text": "先看现金流",
        "quote": {"speaker": "王小明", "self": false, "text": "毛利率：35%，比去年高"}},
       {"type": "text", "text": "……", "marks": ["……"]},
       {"type": "table", "rows": [["公司", "市盈率"], ["甲", "16倍"]]}]}

这里不认 App、不列界面字，只做三件事：

- 校验：像 JSON 却读不出来的抛 ``UnitsError``。模型没按格式交的一段自由文字，和全量重认以前存的旧识图
  （units 为空），都按空行当成几段文字（``plain_units``），不猜说话人、不剥界面字，字照原样留着。
- 排版：一条消息写成「说话人：正文」。截图的人自己发的（聊天里靠右的气泡），和显示名是这个来源作者本人账号的
  （``zsxq_comments.is_owner``，评论区同一个判断），写成方伟，和旧格式同一个写法，别的来源照旧由
  ``relabel_host`` 换成作者名；看不到名字的写网友。被引用的那条写在引用它的那条前面，前面写过的
  （引用框截成前半句的也算）不再写。表格一行一行写。
- 拼接：连着截的几张，后一张开头和前一张结尾重合的那几条只留一份；被上下边缘截掉一部分的那条，
  换成另一张里完整的。被截的那条认乱了、对不上时跳过它，看后面几条对不对得上。
  比的是模型交回的一条条消息和段落，不按版式猜。
  长截图切成的几段（``join_tiles``）知道一定有一截重叠，接缝不按块比，一行行比字（``_seam``）：
  同一截像素，模型在两段里常常分块分得不一样、标的类型不一样，只有一段写了时间这类零碎字。
- 排版：``cut`` 只表示这条在图外还有下文，看得见的字整段留下（和第 3 版一样）。
  只有模型另标 ``illegible``（字被切到笔画中间、只露出半截）时，才不采用它补的那句，
  改写成「被遮挡，无法辨认」，不安到说话人名下。旧行没有这个字段的，有 ``cut`` 也留字。
- 标注：截图后有人另外圈出、框出、划线标出的字（``marks``），排在那一块后面，写成「（图上标出：…）」；
  标出的就是整块时不再抄一遍，写「（图上标出这段）」。字明显比正文小的块（``small``，聊天里转发的书页、
  文章截图）后面写「（小字，可能认错）」：这种字模型常按意思补成通顺的话，又自己觉得看清了，
  字小不小它判断得准，所以让它标字小，摘要和问库看到这行就不把这段当成原话。

结果存在 ``image_texts.units``。以前整理自由文字的那套规则删了，遇到新版式改 ``VISION_PROMPT`` 里的通用说明，
不在这里按 App、按案例补规则。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from researchlib.wechat_dialogue import GUEST, HOST
from researchlib.zsxq_comments import is_owner

UNITS_VERSION = 1
KINDS = frozenset({"chat", "post", "text", "table", "chart", "photo", "other"})
# 有说话人的块。连着截的几张按这些块对齐。
_SPOKEN = "message"
# 显示名长过这个多半不是名字（模型把一句话写进了 speaker），按看不到名字算。
MAX_NAME = 32
# 被截掉一部分的那条，挨着边缘的几个字常常认错。和完整那条比时，末尾（或开头）最多容这么多个字对不上。
EDGE_SLACK = 2
# 重合的几条加起来至少这么多个字，才算后一张是接着前一张截的。只重合一句「好的」不算。
MIN_OVERLAP_CHARS = 8
# 一条被上下两张各截了一半时，两半至少要重合这么多个字才接成一条。
MIN_SPLICE_CHARS = 4
# 长截图的接缝（join_tiles）按行比字（见 ``_seam``）。重叠那一截里只有一段写了的零碎字（时间、地点、点赞数），
# 每处不超过这么多个字，仍算两段对上了；只在一段里的字多过这些，就不当重叠、两边都留着。
SEAM_SLACK = 20
# 接缝处前一段往回、后一段往后各看这么多行。重叠那一截是宽的 0.4 倍，十来行。
SEAM_LINES = 40
# 重叠那一截两段各认一遍，同一行常有一两个字认得不一样（11-11 抽查：一段「估值杀降」，另一段「估值杀跌」）。
# 一行每 NEAR_PER 个字容一个字认差（增、删、换都算一个），最多 NEAR_EDITS 个；不到 NEAR_PER 个字的要一字不差。
# 数字（含「一二三」这类）认得不一样不容：「第一季度」和「第二季度」、「23%」和「28%」是两行。
# 这样对上的行，同一截里还要有一行一字不差地对上才算（``_seam_chain_ok``）：宁可重复，不丢字。
NEAR_PER = 15
NEAR_EDITS = 2
# 字明显比正文小的块（small）后面多写的一行。
SMALL_NOTE = "（小字，可能认错）"
# 字被切到笔画中间（illegible）时，不采用模型补的那句。cut 本身不触发这句：cut 只表示图外还有下文。
CUT_UNREADABLE = {
    "bottom": "（末尾一条被遮挡，无法辨认）",
    "top": "（开头一条被遮挡，无法辨认）",
    "": "（有一条字只露出半截，无法辨认）",
}

_FENCE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\n?(?P<body>.*?)\n?```$", re.S)
_NOT_WORD = re.compile(r"[\W_]+")
_BLANKS = re.compile(r"\n{3,}")
# 控制字符（换行、制表符以外）和双向控制符。模型偶尔交回 NUL，写库时 psycopg 报
# 「text fields cannot contain NUL」，识图整批中断（审核 C4）；别的控制字符在页面上也只是乱码。
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


def strip_control(text: str) -> str:
    """去掉控制字符和双向控制符，换行、回车、制表符留着。"""
    return _CONTROL.sub("", text)
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")
_TRUE = frozenset({"true", "1", "yes", "是"})
_CUTS = {"top": "top", "bottom": "bottom", "上": "top", "下": "bottom"}


class UnitsError(ValueError):
    """回答像 JSON，却读不出符合格式的结构。"""


@dataclass(frozen=True)
class ImageText:
    """一张图的识别结果。units 为空的是全量重认以前存的旧识图（只有 text），排版时当成几段文字（``structure_of``）。"""

    digest: str
    text: str
    units: dict | None = None


def plain_units(text: str) -> dict:
    """没有结构的字当成几段文字：按空行分段，不猜谁说的、哪行是界面，字照原样留着。

    模型偶尔不按格式交回一段自由文字（这次的钱不白花），全量重认以前存的旧识图 units 也是空的。
    """
    raw = (text or "").strip()
    fenced = _FENCE.match(raw)
    if fenced:
        raw = fenced.group("body")
    blocks = [{"type": "text", "text": part} for part in _PARAGRAPH_BREAK.split(raw)]
    return normalize_units({"kind": "other", "blocks": blocks})


def structure_of(record: ImageText) -> dict:
    """排版、拼接、切块用的结构：有 units 用 units，旧识图当成几段文字。"""
    return record.units if record.units is not None else plain_units(record.text)


def parse_reply(raw: str) -> dict | None:
    """模型的回答 → 规整过的 units。

    回答里没有 ``"blocks"`` 也没有 ``"kind"``：模型没按格式交，返回 None，调用方按几段文字存（``plain_units``）。
    有这两个字段却读不出来（多半是截断了）：抛 ``UnitsError``，调用方记失败，不把半截 JSON 当字存。
    """
    text = (raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group("body").strip()
    if '"blocks"' not in text and '"kind"' not in text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise UnitsError("识图回的 JSON 不完整")
    try:
        # strict=False：字符串里直接换行也收。模型常这样写多行消息。
        data = json.loads(text[start : end + 1], strict=False)
    except json.JSONDecodeError as exc:
        raise UnitsError(f"识图回的 JSON 读不出来：{exc}") from exc
    return normalize_units(data)


def normalize_units(data) -> dict:
    """校验并规整：认不出的块丢掉，空字去掉，显示名、截断标记收成固定写法。"""
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise UnitsError("识图回的 JSON 里没有 blocks 列表")
    kind = str(data.get("kind") or "").strip().lower()
    blocks = [block for block in (_block(item) for item in data["blocks"]) if block]
    return {"v": UNITS_VERSION, "kind": kind if kind in KINDS else "other", "blocks": blocks}


def _text(value) -> str:
    if isinstance(value, list):
        value = "\n".join(str(part) for part in value if part is not None)
    if value is None or isinstance(value, (dict, bool)):
        return ""
    lines = [line.strip() for line in strip_control(str(value)).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _name(value) -> str:
    """照图上抄的显示名。冒号换成空格：后面一律按「说话人：正文」拆，名字里不能再有冒号。"""
    if not isinstance(value, str):
        return ""
    name = re.sub(r"\s+", " ", strip_control(value)).strip().lstrip("@＠").strip()
    name = re.sub(r"[:：]+", " ", name).strip()
    if not name or len(name) > MAX_NAME or "\n" in name:
        return ""
    return name


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


def _illegible(item: dict) -> bool:
    """字被切到笔画中间。``partial_glyphs`` 是同一个意思的别名。没有这个字段就不是。"""
    return _flag(item.get("illegible")) or _flag(item.get("partial_glyphs"))


def _cut(value) -> str:
    return _CUTS.get(str(value or "").strip().lower(), "")


def _marks(value) -> list[str]:
    """截图后有人另外圈出、框出、划线标出的几处字，一处一项，去掉空的和重复的。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    found: list[str] = []
    for item in value:
        mark = re.sub(r"\s+", " ", _text(item)).strip()
        if mark and mark not in found:
            found.append(mark)
    return found


def _spoken(value) -> str:
    """一条消息写成一段：消息里的空行收成换行，后面按空行分段时不会把一条拆成两个人说的。"""
    return _PARAGRAPH_BREAK.sub("\n", _text(value))


def _quote(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    text = _spoken(value.get("text"))
    if not text:
        return None
    return {"speaker": _name(value.get("speaker")), "self": _flag(value.get("self")), "text": text}


def _cell(value) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip()


def _block(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "").strip().lower()
    marks = _marks(item.get("marks"))
    if kind == "table":
        rows = [
            [_cell(cell) for cell in row]
            for row in (item.get("rows") if isinstance(item.get("rows"), list) else [])
            if isinstance(row, list)
        ]
        rows = [row for row in rows if any(row)]
        if not rows:
            return None
        table: dict = {"type": "table", "rows": rows}
        if marks:
            table["marks"] = marks
        if _flag(item.get("small")):
            table["small"] = True
        return table
    cut = _cut(item.get("cut"))
    illegible = _illegible(item)
    if kind == _SPOKEN:
        text = _spoken(item.get("text"))
        quote = _quote(item.get("quote"))
        if not text and not quote and not illegible:
            return None
        block = {"type": _SPOKEN, "speaker": _name(item.get("speaker")), "self": _flag(item.get("self")), "text": text}
        if quote:
            block["quote"] = quote
    else:
        # 认不出的类型有字就当成段落：宁可多留一段，不丢字。
        text = _text(item.get("text"))
        if not text and not illegible:
            return None
        block = {"type": "text", "text": text}
    if cut:
        block["cut"] = cut
    if illegible:
        block["illegible"] = True
    if marks:
        block["marks"] = marks
    if _flag(item.get("small")):
        block["small"] = True
    return block


def speaker_label(block: dict, owners: tuple[str, ...] = ()) -> str:
    """消息块写在冒号前面的名字。看不到名字是网友。

    截图的人自己发的（self），和显示名是来源作者本人账号的（owners 是 ``source_owners``：author 加 poster_names），
    写成方伟，再由 relabel_host 换成这篇的作者。owners 为空时只认 self（单张图存 image_texts.text 时不知道来源）。
    帖子、评论里标着「作者」「楼主」的是那个帖子的作者，常常是别人，所以识图只把靠右的气泡标成 self。
    """
    speaker = block.get("speaker") or ""
    if block.get("self") or (owners and speaker and is_owner(speaker, owners)):
        return HOST
    return speaker or GUEST


def has_dialogue(units: dict | None) -> bool:
    return bool(units) and any(block.get("type") == _SPOKEN for block in units.get("blocks") or [])


def _norm(text: str) -> str:
    """比是不是同一句：全角半角、大小写、空白和标点都不算。"""
    return _NOT_WORD.sub("", unicodedata.normalize("NFKC", text or "").lower())


def _body(block: dict) -> str:
    # 笔画被切到中间时，模型补的那句不能拿去和邻图对同一条，否则会把编出来的字拼进去。
    if block.get("illegible"):
        return ""
    if block.get("type") == "table":
        return "\n".join(" | ".join(row) for row in block.get("rows") or [])
    return block.get("text") or ""


def _same_speaker(left: dict, right: dict) -> bool:
    if bool(left.get("self")) != bool(right.get("self")):
        return False
    a, b = _norm(left.get("speaker") or ""), _norm(right.get("speaker") or "")
    return not a or not b or a == b


def _common_prefix(left: str, right: str) -> int:
    size = min(len(left), len(right))
    return next((index for index in range(size) if left[index] != right[index]), size)


def _head_of(whole: str, head: str) -> bool:
    """head 是 whole 的开头：被下边缘截掉一部分的那条，最后几个字可能认错。"""
    if whole.startswith(head):
        return True
    return len(head) > 3 * EDGE_SLACK and _common_prefix(whole, head) >= len(head) - EDGE_SLACK


def _tail_of(whole: str, tail: str) -> bool:
    """tail 是 whole 的结尾：被上边缘截掉一部分的那条，最前面几个字可能认错。"""
    return _head_of(whole[::-1], tail[::-1])


def _pair_matches(upper: dict, lower: dict, *, upper_cut: bool, lower_cut: bool) -> bool:
    """前一张的一块和后一张的一块是不是同一条。upper_cut / lower_cut：这一块挨着图的边缘、被截掉了一部分。"""
    if upper.get("type") != lower.get("type"):
        return False
    if upper.get("type") == _SPOKEN and not _same_speaker(upper, lower):
        return False
    a, b = _norm(_body(upper)), _norm(_body(lower))
    if a == b:
        return True
    if not a or not b or upper.get("type") == "table":
        return False
    if upper_cut and not lower_cut:
        return _head_of(b, a)
    if lower_cut and not upper_cut:
        return _tail_of(a, b)
    return False


def _splice(head: str, tail: str) -> str:
    """一条被上下两张各截了一半：前一半的结尾和后一半的开头重合的部分只留一份。对不上返回空串。"""
    for size in range(min(len(head), len(tail)), MIN_SPLICE_CHARS - 1, -1):
        if head.endswith(tail[:size]):
            return head + tail[size:]
    return ""


def _overlap(upper: list[dict], lower: list[dict]) -> int:
    """后一张开头有几块和前一张结尾重合：同一个人说的同样几条，加起来至少 MIN_OVERLAP_CHARS 个字。不重合是 0。"""
    for size in range(min(len(upper), len(lower)), 0, -1):
        pairs = list(zip(upper[len(upper) - size :], lower[:size], strict=True))
        if not all(
            _pair_matches(
                above,
                below,
                upper_cut=index == size - 1 and above.get("cut") == "bottom",
                lower_cut=index == 0 and below.get("cut") == "top",
            )
            for index, (above, below) in enumerate(pairs)
        ):
            continue
        if sum(max(len(_norm(_body(above))), len(_norm(_body(below)))) for above, below in pairs) >= MIN_OVERLAP_CHARS:
            return size
    return 0


def _after_overlap(upper: list[dict], lower: list[dict], size: int) -> list[dict]:
    """后一张去掉开头重合的 size 块。前一张最后一块被下边缘截过、后一张里是完整的，换成完整的（就地改 upper）。"""
    above, below = upper[-1], lower[size - 1]
    if above.get("cut") == "bottom" and below.get("cut") != "top":
        upper[-1] = below
    return lower[size:]


def _same_kind(left: dict, right: dict) -> bool:
    """同一类块；消息还得是同一个人说的。"""
    if left.get("type") != right.get("type"):
        return False
    return left.get("type") != _SPOKEN or _same_speaker(left, right)


def _join(upper: list[dict], lower: list[dict]) -> list[dict]:
    """后一张去掉和前一张结尾重合的几块。前一张里被截的那块换成后一张里完整的（就地改 upper）。"""
    size = _overlap(upper, lower)
    if size:
        return _after_overlap(upper, lower, size)
    above, below = upper[-1], lower[0]
    if (
        above.get("cut") == "bottom"
        and below.get("cut") == "top"
        and above.get("type") == below.get("type") != "table"
        and (above.get("type") != _SPOKEN or _same_speaker(above, below))
    ):
        whole = _splice(above.get("text") or "", below.get("text") or "")
        if whole:
            upper[-1] = {**{key: value for key, value in above.items() if key != "cut"}, "text": whole}
            return lower[1:]
    # 挨着边缘、只露出一部分的那块常常认乱（被标题栏挡住半行，认出来是另一句话），和另一张里完整的那条对不上，
    # 后面重合的几条就多写一遍。跳过它再比：后面几块对得上，它就是另一张里紧挨着那条的残片，
    # 同一个人说的才算，去掉残片，留完整的。对不上就照旧都留着：宁可重复，不丢字。
    if below.get("cut") == "top" and len(lower) > 1:
        size = _overlap(upper, lower[1:])
        if size and len(upper) > size and _same_kind(upper[-size - 1], below):
            return _after_overlap(upper, lower[1:], size)
    if above.get("cut") == "bottom" and len(upper) > 1:
        size = _overlap(upper[:-1], lower)
        if size and len(lower) > size and _same_kind(above, lower[size]):
            upper.pop()
            return lower[size:]
    return lower


def stitch(units_list: list[dict | None]) -> list[list[dict] | None]:
    """按图片顺序，每张图拼接以后还要写的块；旧格式的图是 None。不改传进来的 units。

    只拼挨着的两张。中间隔着旧格式的图、或者一张没有字的图，就不算连着截的。
    后一张整张都和前一张重合时，再下一张接着和前一张比。
    """
    result: list[list[dict] | None] = []
    upper: list[dict] | None = None
    for units in units_list:
        if units is None:
            result.append(None)
            upper = None
            continue
        blocks = [dict(block) for block in units.get("blocks") or []]
        if not blocks:
            result.append([])
            upper = None
            continue
        if upper:
            blocks = _join(upper, blocks)
        result.append(blocks)
        if blocks:
            upper = blocks
    return result


# join_tiles 里临时记在块上：这一块挨着整张图真正的上边或下边，模型标了 cut。接完以后只留这种 cut。
_EDGE = "_edge"


@dataclass(frozen=True)
class _Line:
    """接缝处拿来比的一行：第几块、哪个字段（quote 引用框、text 自己的字、rows 表格）的第几行。"""

    block: int
    field: str
    index: int
    raw: str
    key: str


@dataclass(frozen=True)
class _Match:
    """前一段的几行（upper，含两头）和后一段的几行（lower）是同一截字。

    how：same 字一样（一行对一行，或一边的一行是另一边接起来的几行）；tail 后一段第一行是前一段那行的后半截；
    head 前一段最后一行只到接缝，是后一段那行的前半截；splice 一行跨过整个重叠，两段各有一半，中间重合。
    weight 是对上的字数。near：两边认差了一两个字（``NEAR_PER``），不是一字不差。
    """

    upper: tuple[int, int]
    lower: tuple[int, int]
    weight: int
    how: str
    near: bool = False


def _fields(block: dict) -> list[tuple[str, list[str]]]:
    """一块按阅读顺序拆成几行：引用框的字（排版时写在前面）、自己的字、表格一行行。

    字只露出半截的（illegible）不拿它自己那句来比：那句是模型补的。
    """
    if block.get("type") == "table":
        return [("rows", [" | ".join(row) for row in block.get("rows") or []])]
    fields: list[tuple[str, list[str]]] = []
    quote = block.get("quote")
    if quote and quote.get("text"):
        fields.append(("quote", quote["text"].split("\n")))
    if block.get("text") and not block.get("illegible"):
        fields.append(("text", block["text"].split("\n")))
    return fields


def _lines(blocks: list[dict]) -> list[_Line]:
    return [
        _Line(number, field, index, raw, key)
        for number, block in enumerate(blocks)
        for field, rows in _fields(block)
        for index, raw in enumerate(rows)
        if (key := _norm(raw))
    ]


def _key_overlap(head: str, tail: str) -> int:
    """head 的结尾和 tail 的开头重合几个字（比两边都短，至少 MIN_SPLICE_CHARS），不重合是 0。"""
    for size in range(min(len(head), len(tail)) - 1, MIN_SPLICE_CHARS - 1, -1):
        if head.endswith(tail[:size]):
            return size
    return 0


def _edits(short: str, long: str, budget: int, *, prefix: bool = False) -> int:
    """short 改成 long（prefix 时改成 long 的某个开头）最少改几个字；超过 budget 交回 budget + 1。

    增、删、换一个字各算 1。数字（``str.isnumeric``，含「一二三」）对不上算超出：认差的是数字就不当同一行。
    只算对角线两边各 budget 宽的一条带，一超出就停，所以两行字不一样时很快就停。
    """
    over = budget + 1
    if prefix:
        long = long[: len(short) + budget]
    size, width = len(short), len(long)
    if width < size - budget or (not prefix and width > size + budget):
        return over

    def step(char: str) -> int:
        return over if char.isnumeric() else 1

    row = [over] * (width + 1)
    row[0] = 0
    for j in range(1, min(width, budget) + 1):
        row[j] = min(over, row[j - 1] + step(long[j - 1]))
    for i in range(1, size + 1):
        low, high = max(0, i - budget), min(width, i + budget)
        new = [over] * (width + 1)
        if low == 0:
            new[0] = min(over, row[0] + step(short[i - 1]))
        for j in range(max(1, low), high + 1):
            left, right = short[i - 1], long[j - 1]
            if left == right:
                cost = row[j - 1]
            else:
                cost = row[j - 1] + (over if left.isnumeric() or right.isnumeric() else 1)
            new[j] = min(over, cost, row[j] + step(left), new[j - 1] + step(right))
        if min(new[low : high + 1]) >= over:
            return over
        row = new
    if prefix:
        return min(row[max(0, size - budget) : width + 1])
    return row[width]


def _near(budget_of: str, short: str, long: str, *, prefix: bool = False) -> int:
    """short 和 long（prefix 时是 long 的开头）是同一行、认差了几个字：交回差几个，算不上交回 -1。
    能容几个按 budget_of 的字数算（见 ``NEAR_PER``）。"""
    budget = min(NEAR_EDITS, len(budget_of) // NEAR_PER)
    if not budget:
        return -1
    edits = _edits(short, long, budget, prefix=prefix)
    return edits if edits <= budget else -1


def _grouped(key: str, lines: list[_Line], start: int) -> int:
    """lines 从 start 起的几行接起来正好是 key（两段换行换得不一样）：交回最后那行的位置，接不出来是 -1。"""
    joined = ""
    for index in range(start, len(lines)):
        joined += lines[index].key
        if joined == key:
            return index
        if not key.startswith(joined):
            return -1
    return -1


def _seam_matches(up: list[_Line], low: list[_Line]) -> list[_Match]:
    """接缝两边对得上的几行：字一样的，或者一边的一行是另一边几行接起来的（两段换行换得不一样）。
    截一半的只看挨着接缝的那一行：前一段最后一行、后一段第一行，表格行不算。
    一字不差对不上的，再看是不是同一行（或截一半的那行）认差了一两个字（``NEAR_PER``），表格行不算。"""
    found: list[_Match] = []
    last = len(up) - 1
    for i, above in enumerate(up):
        for j, below in enumerate(low):
            if above.key == below.key:
                found.append(_Match((i, i), (j, j), len(above.key), "same"))
                continue
            end = _grouped(above.key, low, j)
            if end > j:
                found.append(_Match((i, i), (j, end), len(above.key), "same"))
            end = _grouped(below.key, up, i)
            if end > i:
                found.append(_Match((i, end), (j, j), len(below.key), "same"))
            if "rows" in (above.field, below.field):
                continue
            if j == 0 and len(below.key) >= MIN_SPLICE_CHARS and _tail_of(above.key, below.key):
                found.append(_Match((i, i), (0, 0), len(below.key), "tail"))
            elif i == last and len(above.key) >= MIN_SPLICE_CHARS and _head_of(below.key, above.key):
                found.append(_Match((i, i), (j, j), len(above.key), "head"))
            elif i == last and j == 0 and (overlap := _key_overlap(above.key, below.key)):
                found.append(_Match((i, i), (0, 0), overlap, "splice"))
            elif (edits := _near(min(above.key, below.key, key=len), above.key, below.key)) >= 0:
                weight = min(len(above.key), len(below.key)) - edits
                found.append(_Match((i, i), (j, j), weight, "same", near=True))
            elif j == 0 and (edits := _near(below.key, below.key[::-1], above.key[::-1], prefix=True)) >= 0:
                found.append(_Match((i, i), (0, 0), len(below.key) - edits, "tail", near=True))
            elif i == last and (edits := _near(above.key, above.key, below.key, prefix=True)) >= 0:
                found.append(_Match((i, i), (j, j), len(above.key) - edits, "head", near=True))
    return found


def _seam_chain(up: list[_Line], low: list[_Line], upper: list[dict], lower: list[dict]) -> list[_Match]:
    """两段重叠的那一截：前一段结尾的几行和后一段开头的几行一行行对上。对不上返回空列表。

    中间可以有只在一段里的行（一段写了时间、点赞数，另一段没写；或者两段认得不一样），每处不超过 SEAM_SLACK 个字：
    后一段第一处对上的行前面、前一段最后一处对上的行后面、两处对上的行之间都这样算。
    这样的对法里挑对上的字最多、夹着的零碎字最少的。对上的字少于 MIN_OVERLAP_CHARS 的，只认两种：
    前一段最后几行和后一段开头几行一行不差地挨着对上两行以上；或者只对上一行，那一行在两段里是同一类块、同一个人说的。
    """
    if not up or not low:
        return []
    matches = sorted(_seam_matches(up, low), key=lambda match: (match.upper[1], match.lower[1]))
    before_up = [0]
    for line in up:
        before_up.append(before_up[-1] + len(line.key))
    before_low = [0]
    for line in low:
        before_low.append(before_low[-1] + len(line.key))
    # 每一种对法记 (得分, 对上的字数, 上一处对上的是哪个)。得分 = 对上的字 - 夹着的零碎字。
    best: list[tuple[int, int, int | None] | None] = []
    for number, match in enumerate(matches):
        (i0, _i1), (j0, _j1) = match.upper, match.lower
        options: list[tuple[int, int, int | None]] = []
        if before_low[j0] <= SEAM_SLACK:
            options.append((match.weight - before_low[j0], match.weight, None))
        for previous in range(number):
            found = best[previous]
            earlier = matches[previous]
            if found is None or earlier.upper[1] >= i0 or earlier.lower[1] >= j0:
                continue
            gap_low = before_low[j0] - before_low[earlier.lower[1] + 1]
            if gap_low > SEAM_SLACK:
                continue
            gap_up = before_up[i0] - before_up[earlier.upper[1] + 1]
            options.append((found[0] + match.weight - gap_low - gap_up, found[1] + match.weight, previous))
        best.append(max(options, key=lambda option: option[:2]) if options else None)
    picked: tuple[tuple[int, int, int], list[_Match]] | None = None
    for number, found in enumerate(best):
        if found is None:
            continue
        trail = before_up[len(up)] - before_up[matches[number].upper[1] + 1]
        if trail > SEAM_SLACK:
            continue
        chain: list[_Match] = []
        step: int | None = number
        while step is not None:
            chain.append(matches[step])
            step = best[step][2]  # type: ignore[index]
        chain.reverse()
        if not _seam_chain_ok(chain, found[1], trail, up, low, upper, lower):
            continue
        key = (found[0] - trail, found[1], -matches[number].lower[1])
        if picked is None or key > picked[0]:
            picked = (key, chain)
    return picked[1] if picked else []


def _seam_chain_ok(
    chain: list[_Match],
    weight: int,
    trail: int,
    up: list[_Line],
    low: list[_Line],
    upper: list[dict],
    lower: list[dict],
) -> bool:
    """对上的字够多就算；少于 MIN_OVERLAP_CHARS 的要求见 ``_seam_chain``。
    只有认差了字才对上的（``_Match.near``）不算：同一截里至少要有一行一字不差。"""
    if all(match.near for match in chain):
        return False
    if weight >= MIN_OVERLAP_CHARS:
        return True
    if trail or chain[0].lower[0] != 0:
        return False
    if any(
        later.upper[0] != earlier.upper[1] + 1 or later.lower[0] != earlier.lower[1] + 1
        for earlier, later in zip(chain, chain[1:], strict=False)
    ):
        return False
    if len(chain) > 1:
        return True
    only = chain[0]
    return weight >= MIN_SPLICE_CHARS and _same_kind(upper[up[only.upper[0]].block], lower[low[only.lower[0]].block])


def _raw_after(raw: str, size: int) -> int:
    """raw 里比到第 size 个字（按 ``_norm`` 数）之后的位置。"""
    count = 0
    for index, char in enumerate(raw):
        if count >= size:
            return index
        count += len(_norm(char))
    return len(raw)


def _splice_raw(head: str, tail: str, size: int) -> str:
    """一行跨过接缝：前一段的字接上后一段重合部分之后的字。重合处的标点留前一段的。"""
    rest = tail[_raw_after(tail, size) :]
    if head and not _norm(head[-1]):
        rest = rest.lstrip()
        while rest and not _norm(rest[0]):
            rest = rest[1:]
    return head + rest


def _with_line(block: dict, field: str, index: int, raw: str) -> dict:
    """换掉一块里的一行，不改传进来的块。"""
    if field == "quote":
        rows = block["quote"]["text"].split("\n")
        rows[index] = raw
        return {**block, "quote": {**block["quote"], "text": "\n".join(rows)}}
    rows = block["text"].split("\n")
    rows[index] = raw
    return {**block, "text": "\n".join(rows)}


def _rest_of(block: dict, field: str, index: int) -> dict | None:
    """一块在 (field, index) 那一行以后剩下的部分，连同块上的标注；什么都不剩是 None。"""
    rest = {key: value for key, value in block.items() if key not in ("text", "quote", "rows")}
    if field == "rows":
        rows = (block.get("rows") or [])[index + 1 :]
        return {**rest, "rows": rows} if rows else None
    quote = block.get("quote")
    if field == "quote" and quote:
        lines = quote["text"].split("\n")[index + 1 :]
        if any(line.strip() for line in lines):
            rest["quote"] = {**quote, "text": "\n".join(lines).strip()}
        text = block.get("text") or ""
    else:
        text = "\n".join((block.get("text") or "").split("\n")[index + 1 :]).strip()
    if block.get("type") == _SPOKEN:
        if not text and "quote" not in rest:
            return None
        rest["text"] = text
        return rest
    return {**rest, "text": text} if text else None


def _merged_marks(upper: dict, lower: dict) -> list[str]:
    marks = list(upper.get("marks") or [])
    return marks + [mark for mark in lower.get("marks") or [] if mark not in marks]


def _continue_block(block: dict, rest: dict) -> dict | None:
    """后一段一块在重叠以后还有字，前一段最后一块正好是它重叠前的部分：接到前一段那块后面。

    表格接表格的行，消息接同一个人的消息（后一段看不到名字的也算），段落接段落；说话人按前一段的
    （前一段看得到这条从哪开始）。接不上（类型不同、说话人不同、剩下的还带着引用框）返回 None，另起一块。
    """
    table = block.get("type") == "table"
    if not _same_kind(block, rest) or "quote" in rest:
        return None
    if table:
        joined = {**block, "rows": [*(block.get("rows") or []), *rest["rows"]]}
    else:
        joined = {**block, "text": "\n".join(part for part in (block.get("text") or "", rest["text"]) if part)}
    marks = _merged_marks(block, rest)
    if marks:
        joined["marks"] = marks
    if rest.get("small"):
        joined["small"] = True
    if rest.get(_EDGE):
        joined[_EDGE] = rest[_EDGE]
    return joined


def _seam(upper: list[dict], lower: list[dict]) -> list[dict]:
    """长截图相邻两段在重叠处只留一份（就地改 upper），交回后一段剩下的块。

    两段看到的是同一截像素，可模型在两段里常常分块分得不一样（一段把海报 4 行当一块，另一段分成 3 行加 1 行）、
    标的类型不一样（一段标成消息，另一段标成段落）、零碎字只有一段写了（时间、点赞数），按块比就对不上，
    重叠那一截写两遍。所以这里不看块怎么分、标的什么，一行行比字（``_seam_chain``）。
    对上的那一截留前一段的写法（前一段看得到每条从哪开始、是谁说的），后一段对上以前的行都去掉；
    前一段最后一行只到接缝的，换成后一段里完整的那行；一行跨过整个重叠的，两半接成一行。
    后一段最后对上的那块在重叠以后还有字、前一段最后一块正好是它的前面部分，接到前一段那块后面，
    说话人跟着前一段（后一段从中间开始，常看不到这条是谁说的）。对不上就都留着：宁可重复，不丢字。
    """
    up = _lines(upper)[-SEAM_LINES:]
    low = _lines(lower)[:SEAM_LINES]
    chain = _seam_chain(up, low, upper, lower)
    if not chain:
        return lower
    last = chain[-1]
    above, below = up[last.upper[1]], low[last.lower[1]]
    if last.how in ("head", "splice"):
        whole = below.raw if last.how == "head" else _splice_raw(above.raw, below.raw, last.weight)
        upper[above.block] = _with_line(upper[above.block], above.field, above.index, whole)
    kept = lower[below.block + 1 :]
    rest = _rest_of(lower[below.block], below.field, below.index)
    if rest is None:
        return kept
    if above.block == len(upper) - 1 and last.upper[1] == len(up) - 1:
        joined = _continue_block(upper[-1], rest)
        if joined is not None:
            upper[-1] = joined
            return kept
    return [rest, *kept]


def join_tiles(parts: list[dict]) -> dict:
    """一张长截图切成几段分别认（``researchlib.ocr.tile_spans``）以后，接回这一张图的 units。

    相邻两段重叠的那一截两段里都有，按行比字只留一份（``_seam``）：不看两段把字分成几块、标成什么，
    挨着接缝只露一半的那行换成另一段里完整的，跨过接缝的一段接成一段，表格切开的接成一张表。
    接缝处模型标了 illegible 的那条（切口压着字，只露出半截笔画）直接去掉：两段重叠宽的 0.4 倍，
    这一行在另一段里整行都看得见，不写「被遮挡，无法辨认」。整张图真正上下边的 illegible 照旧留着。
    接完只留整张图上下边真截到的 cut（第一段第一块的 top、最后一段最后一块的 bottom），接缝处的都去掉。
    kind 取块最多的那一段的；tiles 记切了几段；parts 原样存每段认出来的结构：接法再改，
    ``research-ocr --rejoin`` 按它重接，不用再花钱认；``research-ocr --tall`` 也靠它认出哪些长图已经切段认过。
    """
    result: list[list[dict]] = []
    upper: list[dict] | None = None
    last = len(parts) - 1
    for index, units in enumerate(parts):
        blocks = [dict(block) for block in (units or {}).get("blocks") or []]
        while index > 0 and blocks and blocks[0].get("illegible"):
            blocks.pop(0)
        while index < last and blocks and blocks[-1].get("illegible"):
            blocks.pop()
        if not blocks:
            result.append([])
            upper = None
            continue
        if index == 0 and blocks[0].get("cut") == "top":
            blocks[0][_EDGE] = "top"
        if index == last and blocks[-1].get("cut") == "bottom":
            blocks[-1][_EDGE] = "bottom"
        if upper:
            blocks = _seam(upper, blocks)
        result.append(blocks)
        if blocks:
            upper = blocks
    joined = [block for blocks in result for block in blocks]
    for block in joined:
        edge = block.pop(_EDGE, "")
        block.pop("cut", None)
        if edge:
            block["cut"] = edge
    kinds = [
        (len((units or {}).get("blocks") or []), -index, (units or {}).get("kind") or "other")
        for index, units in enumerate(parts)
    ]
    kind = max(kinds)[2] if kinds else "other"
    return {
        "v": UNITS_VERSION,
        "kind": kind if kind in KINDS else "other",
        "blocks": joined,
        "tiles": len(parts),
        "parts": [units or {} for units in parts],
    }


def _written_before(text: str, written: set[str]) -> bool:
    """引用的那条前面写过。引用框常把原话截成前半句加省略号，前半句是前面某条的开头也算写过。"""
    key = _norm(text)
    if key in written:
        return True
    return len(key) >= MIN_OVERLAP_CHARS and any(_head_of(done, key) for done in written)


def _marks_note(block: dict) -> str:
    """标出的字写成一行。标出的是整块就不再抄一遍：「（图上标出这段）」，消息写「这条」。"""
    marks = block.get("marks") or []
    if not marks:
        return ""
    whole = _norm(_body(block))
    if whole and (whole == _norm("".join(marks)) or any(_norm(mark) == whole for mark in marks)):
        return "（图上标出这条）" if block.get("type") == _SPOKEN else "（图上标出这段）"
    return f"（图上标出：{'；'.join(marks)}）"


def _notes(block: dict) -> str:
    """写在一块后面的说明：小字一行，标注一行。"""
    return "\n".join(part for part in (SMALL_NOTE if block.get("small") else "", _marks_note(block)) if part)


def paragraphs(
    blocks: list[dict], written: set[str] | None = None, owners: tuple[str, ...] = ()
) -> list[tuple[str, list[str]]]:
    """一张图的块排成段：[(说话人, [一行一行的字])]，没有说话人的段落和表格说话人是空串。

    同一个人连着说的并成一段，网友每次开口各自一段（看不到名字的不一定是同一个人）。
    字明显比正文小的块（small）后面写一行「（小字，可能认错）」。
    有人另外标出的字（marks）写在那一块后面一行：「（图上标出：…）」，标出的是整块时写「（图上标出这段）」。
    cut 的字照写。illegible 的不写模型那句，改成「被遮挡，无法辨认」，不安到说话人名下。
    written：前面已经写过的消息（就地更新）。引用的那条前面写过（截成前半句的也算），就不再写一遍。
    owners 见 ``speaker_label``。
    """
    written = set() if written is None else written
    entries: list[tuple[str, str]] = []
    for block in blocks:
        if block.get("illegible") and block.get("type") != "table":
            # 字只露出半截。cut 不走这里：cut 的字是看得清的，整段留下。
            if block.get("type") == _SPOKEN:
                quote = block.get("quote")
                if quote and not _written_before(quote["text"], written):
                    entries.append((speaker_label(quote, owners), quote["text"]))
                    written.add(_norm(quote["text"]))
            if block.get("text"):
                entries.append(("", CUT_UNREADABLE.get(block.get("cut") or "", CUT_UNREADABLE[""])))
            continue
        note = _notes(block)
        if block.get("type") != _SPOKEN:
            entries.append(("", "\n".join(part for part in (_body(block), note) if part)))
            continue
        quote = block.get("quote")
        if quote and not _written_before(quote["text"], written):
            entries.append((speaker_label(quote, owners), quote["text"]))
            written.add(_norm(quote["text"]))
        if block.get("text"):
            entries.append((speaker_label(block, owners), "\n".join(part for part in (block["text"], note) if part)))
            written.add(_norm(block["text"]))
    grouped: list[tuple[str, list[str]]] = []
    for label, text in entries:
        if grouped and label and label != GUEST and grouped[-1][0] == label:
            grouped[-1][1].append(text)
            continue
        grouped.append((label, [text]))
    return grouped


def format_blocks(blocks: list[dict], written: set[str] | None = None, owners: tuple[str, ...] = ()) -> str:
    """一张图的块写成旁路文字：「说话人：正文」，段与段之间空一行。和旧格式整理好的样子同一个写法。"""
    return "\n\n".join(
        f"{label}：" + "\n".join(texts) if label else "\n".join(texts)
        for label, texts in paragraphs(blocks, written, owners)
    ).strip()


def format_units(units: dict | None) -> str:
    """一张图单独写成文字（存进 image_texts.text，读旧列的地方照样能用）。"""
    return format_blocks(list((units or {}).get("blocks") or []))

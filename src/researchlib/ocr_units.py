"""识图的结构化结果：模型按 JSON 交回图里有什么，这里只校验、排版，把连着截的几张拼起来。

以前识图交回一段自由文字，后面由 ``wechat_dialogue`` 用几十条正则去猜：哪行是谁说的、哪行是界面、
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

- 校验：不是 JSON 的回答返回 None，调用方按旧格式存；像 JSON 却读不出来的抛 ``UnitsError``。
- 排版：一条消息写成「说话人：正文」。截图的人自己发的（聊天里靠右的气泡），和显示名是这个来源作者本人账号的
  （``zsxq_comments.is_owner``，评论区同一个判断），写成方伟，和旧格式同一个写法，别的来源照旧由
  ``relabel_host`` 换成作者名；看不到名字的写网友。被引用的那条写在引用它的那条前面，前面写过的
  （引用框截成前半句的也算）不再写。表格一行一行写。
- 拼接：连着截的几张，后一张开头和前一张结尾重合的那几条只留一份；被上下边缘截掉一部分的那条，
  换成另一张里完整的。被截的那条认乱了、对不上时跳过它，看后面几条对不对得上。
  比的是模型交回的一条条消息和段落，不按版式猜。
- 标注：截图后有人另外圈出、框出、划线标出的字（``marks``），排在那一块后面，写成「（图上标出：…）」；
  标出的就是整块时不再抄一遍，写「（图上标出这段）」。

结果存在 ``image_texts.units``。units 为空的是旧格式，仍由 ``wechat_dialogue`` 的旧规则整理；
那套规则冻结，只管还没重识的旧图，不再往里加。遇到新版式改 ``VISION_PROMPT`` 里的通用说明。
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

_FENCE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\n?(?P<body>.*?)\n?```$", re.S)
_NOT_WORD = re.compile(r"[\W_]+")
_BLANKS = re.compile(r"\n{3,}")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")
_TRUE = frozenset({"true", "1", "yes", "是"})
_CUTS = {"top": "top", "bottom": "bottom", "上": "top", "下": "bottom"}


class UnitsError(ValueError):
    """回答像 JSON，却读不出符合格式的结构。"""


@dataclass(frozen=True)
class ImageText:
    """一张图的识别结果。units 为空是旧格式（只有 text）。"""

    digest: str
    text: str
    units: dict | None = None


def parse_reply(raw: str) -> dict | None:
    """模型的回答 → 规整过的 units。

    回答里没有 ``"blocks"`` 也没有 ``"kind"``：模型没按格式交，返回 None，调用方当旧格式的文字存。
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
    lines = [line.strip() for line in str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _name(value) -> str:
    """照图上抄的显示名。冒号换成空格：后面一律按「说话人：正文」拆，名字里不能再有冒号。"""
    if not isinstance(value, str):
        return ""
    name = re.sub(r"\s+", " ", value).strip().lstrip("@＠").strip()
    name = re.sub(r"[:：]+", " ", name).strip()
    if not name or len(name) > MAX_NAME or "\n" in name:
        return ""
    return name


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


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
        return {"type": "table", "rows": rows, "marks": marks} if marks else {"type": "table", "rows": rows}
    cut = _cut(item.get("cut"))
    if kind == _SPOKEN:
        text = _spoken(item.get("text"))
        quote = _quote(item.get("quote"))
        if not text and not quote:
            return None
        block = {"type": _SPOKEN, "speaker": _name(item.get("speaker")), "self": _flag(item.get("self")), "text": text}
        if quote:
            block["quote"] = quote
    else:
        # 认不出的类型有字就当成段落：宁可多留一段，不丢字。
        text = _text(item.get("text"))
        if not text:
            return None
        block = {"type": "text", "text": text}
    if cut:
        block["cut"] = cut
    if marks:
        block["marks"] = marks
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


def paragraphs(
    blocks: list[dict], written: set[str] | None = None, owners: tuple[str, ...] = ()
) -> list[tuple[str, list[str]]]:
    """一张图的块排成段：[(说话人, [一行一行的字])]，没有说话人的段落和表格说话人是空串。

    同一个人连着说的并成一段，网友每次开口各自一段（看不到名字的不一定是同一个人）。
    有人另外标出的字（marks）写在那一块后面一行：「（图上标出：…）」，标出的是整块时写「（图上标出这段）」。
    written：前面已经写过的消息（就地更新）。引用的那条前面写过（截成前半句的也算），就不再写一遍。
    owners 见 ``speaker_label``。
    """
    written = set() if written is None else written
    entries: list[tuple[str, str]] = []
    for block in blocks:
        note = _marks_note(block)
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

"""识图（OCR）正文里的数据表：从逐行文字里认出表格，交给阅读页排成 <table>。

模型识图把表读成「一行一条记录，单元格之间一个空格」：表头一行，数据每行一条。
按行摊成 ``<p>`` 时表头和数值列对不上，读不出是同一张表。这里只认结构，不补数：
表头是数据行上面那一行同列数的标签行，表头/标题/注记留在表外；认不出就原样返回，仍按行排。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# 单元格里的数值：识图偶尔在数字前留 ?、~、≈，或写成 1,234、10.59%、217.65亿元。
_CELL_NUMBER = re.compile(
    r"^[?~≈<>≤≥+\-−—]?"
    r"[¥$€£￥]?"
    r"\d[\d,]*(?:\.\d+)?"
    r"(?:亿元|万美元|亿美元|万元|美元|港元|人民币|万|亿|倍|元|%|％)?$"
)
# 表里留空的单元格。
_CELL_BLANK = re.compile(r"^[—–\-−/／]+$")
_SENTENCE_END = re.compile(r"[。！？!?…；;]$")
_CJK = re.compile(r"[一-鿿]")
# 表头常常只有年份（「项目 2023 2024」），这种行和一行数据长得一样，靠四位数年份认出来。
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
# 表头单元格：短标签、日期、指标名。长句子和「42.7/60.0」这种数值不当表头。
_HEADER_MAX = 24
_HEADER_PUNCT = re.compile(r"[，、；;：:。！？!?]")
_VALUE_SHAPE = re.compile(r"^[\d.,%％/\\\-−—+()（）~<>≤≥\s*]+$")
# 折行的单元格文字一般只有几个字，长句不当折行。
_WRAP_LABEL_MAX = 30


@dataclass(frozen=True)
class TableRow:
    """表里的一行：行首标签 + 各数据列。"""

    label: str
    cells: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    """认出来的一张表。start/end 是原文本里的行号（``[start, end)``）。"""

    start: int
    end: int
    header: tuple[str, ...] | None
    rows: tuple[TableRow, ...]

    @property
    def width(self) -> int:
        return len(self.rows[0].cells) if self.rows else 0


def _is_cell_value(token: str) -> bool:
    return bool(_CELL_NUMBER.match(token) or _CELL_BLANK.match(token))


def _split_row(line: str) -> TableRow | None:
    """把一行拆成「行首标签 + 末尾连续的数值列」。从右往左数，标签里带数字也不打紧。

    至少两列数才算表格行：一列数的情况（``门店数 123``）在 :func:`_looks_like_table` 里另判。
    """
    tokens = (line or "").split()
    if len(tokens) < 3:
        return None
    tail: list[str] = []
    for token in reversed(tokens):
        if not _is_cell_value(token):
            break
        tail.append(token)
    tail.reverse()
    if len(tail) < 2 or not any(_CELL_NUMBER.match(token) for token in tail):
        return None
    label = " ".join(tokens[: len(tokens) - len(tail)]).strip()
    if not label:
        return None
    return TableRow(label, tuple(tail))


def _split_pair(line: str) -> TableRow | None:
    """两列的「项目 数值」行：只有一个标签和一个数。要求标签不是纯数字，避免把一串数字当表。"""
    tokens = (line or "").split()
    if len(tokens) != 2 or not _CELL_NUMBER.match(tokens[1]) or _CELL_NUMBER.match(tokens[0]):
        return None
    return TableRow(tokens[0], (tokens[1],))


def _header_cells_ok(cells: Sequence[str]) -> bool:
    """表头单元格不能是句子，也不能只有一个数值的形状（``42.7/60.0``）。"""
    for cell in cells:
        if not cell or len(cell) > _HEADER_MAX:
            return False
        if _HEADER_PUNCT.search(cell) or _VALUE_SHAPE.match(cell):
            return False
    return True


def _header_row(line: str, width: int) -> tuple[str, ...] | None:
    """数据行上面那一行：同列数、多一个行首标签，各列都不是数值，也不是一句话。

    第一列没有字（图上那个角是空的）时也认：整行都是带数字的列名，例如「2025年 2024年」。
    """
    tokens = (line or "").split()
    if len(tokens) == width + 1:
        cells = tokens[1:]
        if any(_is_cell_value(token) for token in cells) or not _header_cells_ok(cells):
            return None
        return tuple(tokens)
    if width >= 2 and len(tokens) == width:
        if any(_is_cell_value(token) for token in tokens) or not _header_cells_ok(tokens):
            return None
        if not any(re.search(r"\d", token) for token in tokens):
            return None
        return ("",) + tuple(tokens)
    return None


def _year_header(rows: list[TableRow]) -> bool:
    """数据块的第一行全是四位数年份：这是「项目 2023 2024」那种表头，不是一条数据。"""
    if len(rows) < 2 or not rows[0].cells:
        return False
    if not all(_YEAR.match(cell) for cell in rows[0].cells):
        return False
    return any(not _YEAR.match(cell) for cell in rows[1].cells)


def _wrapped_label(line: str) -> bool:
    """上一行像是单元格文字折到了下一行：括号没合上，也没写完一句话。"""
    text = (line or "").strip()
    if not text or len(text) > _WRAP_LABEL_MAX or _SENTENCE_END.search(text):
        return False
    opens = text.count("（") + text.count("(")
    closes = text.count("）") + text.count(")")
    return opens > closes


def _join_label(head: str, tail: str) -> str:
    """把折行的标签接回去：汉字和括号之间不加空格，拉丁词之间加一个。"""
    if not head:
        return tail
    if not tail:
        return head
    if _CJK.search(head[-1]) or head[-1] in "（(【「《" or _CJK.search(tail[0]) or tail[0] in "）)】」》":
        return head + tail
    return f"{head} {tail}"


def _looks_like_table(width: int, row_count: int, header: tuple[str, ...] | None) -> bool:
    """两列以上：两行就认，或表头下面只有一行。一列数：必须有表头，且至少三行。"""
    if width >= 2:
        return row_count >= 2 or (header is not None and row_count >= 1)
    return header is not None and row_count >= 3


def find_tables(lines: Sequence[str]) -> list[Table]:
    """按出现顺序找出所有表格。表格不能跨空行；相邻同宽、后一张没表头的并成一张。"""
    tables: list[Table] = []
    total = len(lines)
    index = 0
    while index < total:
        first = _split_row(lines[index]) or _split_pair(lines[index])
        if first is None:
            index += 1
            continue
        width = len(first.cells)
        rows = [first]
        end = index + 1
        while end < total:
            nxt = _split_row(lines[end]) or _split_pair(lines[end])
            if nxt is None or len(nxt.cells) != width:
                break
            rows.append(nxt)
            end += 1
        start = index
        header = _header_row(lines[index - 1], width) if index else None
        if header is not None:
            start = index - 1
        elif _year_header(rows):
            header = (rows[0].label,) + rows[0].cells
            rows = rows[1:]
        elif start and _wrapped_label(lines[start - 1]):
            # 单元格文字折到下一行：上一小块并回这一行的行首，不要当成表外的正文。
            rows[0] = TableRow(_join_label(lines[start - 1].strip(), rows[0].label), rows[0].cells)
            start -= 1
        if _looks_like_table(width, len(rows), header):
            table = Table(start, end, header, tuple(rows))
            previous = tables[-1] if tables else None
            if (
                previous is not None
                and previous.end == table.start
                and previous.width == table.width
                and table.header is None
            ):
                tables.pop()
                table = Table(previous.start, table.end, previous.header, previous.rows + table.rows)
            tables.append(table)
        index = end
    return tables

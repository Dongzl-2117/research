"""按有序叶子边界切展示单元，保留引用／列表外壳和非译内容，不靠删除凑段数。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from researchlib.translate.segments import _El, _FragmentParser, _outer, _Tx, iter_segments


@dataclass(frozen=True)
class Unit:
    html: str
    start: int
    stop: int


def units(document: str) -> list[Unit]:
    parser = _FragmentParser()
    parser.feed(document)
    parser.close()
    position = 0

    def walk(node: _El | _Tx) -> list[Unit]:
        nonlocal position
        if isinstance(node, _Tx):
            return [Unit(node.data, position, position)] if node.data.strip() else []
        outer = _outer(node)
        count = len(iter_segments(outer))
        # 表格必须保留行列、合并单元格和表头关系，不能把数字单元格扔掉。
        if count <= 1 or node.name in {"table", "pre", "code"}:
            before = position
            position += count
            return [Unit(outer, before, position)]
        result = []
        list_number = None
        if node.name == "ol":
            match = re.search(r'\bstart=["\'](-?\d+)["\']', node.start)
            reversed_list = bool(re.search(r"\breversed\b", node.start))
            initial = (sum(isinstance(child, _El) and child.name == "li" for child in node.children)
                       if reversed_list else 1)
            list_number = int(match.group(1)) if match else initial
        for child in node.children:
            parts = walk(child)
            start = node.start
            if list_number is not None and isinstance(child, _El) and child.name == "li":
                value = re.search(r'\bvalue=["\'](-?\d+)["\']', child.start)
                if value:
                    list_number = int(value.group(1))
                start = re.sub(r'\s+start=["\'][^"\']*["\']', "", start)
                start = start[:-1] + f' start="{list_number}">'
                list_number += -1 if re.search(r"\breversed\b", node.start) else 1
            for index, part in enumerate(parts):
                opening = start
                if node.name == "li" and index:
                    if re.search(r'''\bclass=["']''', start):
                        opening = re.sub(r'''(\bclass=["'])([^"']*)''', r"\1\2 tx-list-continuation", start)
                    else:
                        opening = start[:-1] + ' class="tx-list-continuation">'
                result.append(Unit(f"{opening}{part.html}</{node.name}>", part.start, part.stop))
        return result

    result = []
    for item in parser.root.children:
        result.extend(walk(item))
    return result


def paired_units(original: str, translated: str) -> list[tuple[str, str]] | None:
    left, right = units(original), units(translated)
    en_total = max((u.stop for u in left), default=0)
    zh_total = max((u.stop for u in right), default=0)
    if en_total != zh_total:
        return None
    if not en_total:
        return [(original, translated)]
    boundaries = sorted(({u.stop for u in left} & {u.stop for u in right}) | {0, en_total})

    def take(items: list[Unit], start: int, stop: int) -> str:
        return "".join(u.html for u in items if u.start >= start and u.stop <= stop
                       and (u.start < stop or stop == en_total))

    return [(take(left, start, stop), take(right, start, stop))
            for start, stop in zip(boundaries, boundaries[1:], strict=False)]

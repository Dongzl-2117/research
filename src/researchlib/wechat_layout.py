"""方伟公众号以前入库的文章，图按公众号原文页的位置摆：字一个不改，只挪单独成行的图。

    docker compose exec app research-place-wechat-images --source fangwei-mp --dry-run   # 只看能挪几篇
    docker compose exec app research-place-wechat-images --source fangwei-mp --limit 50

11 月 7 日以前的抓取取的是极致了的正文：第 N 张图放在第 N 段后面，图常整组堆在开头的引文里，
阅读页只好按识图的字猜回去（render._relocate_images）。原文页（``#js_content``）上图在哪就是哪，
这里拿它当准：

- 两边的字只比字母、数字和汉字（标点、空白、加粗、链接地址都不算），用 difflib 对齐，
  把原文页上每张图前面的那个字对到归档正文里，图放到离它最近的行与行之间。行里的字不拆、不改。
- 哪张图对原文页上哪张：图床地址里图片的 id 对得上就按 id，对不上、或者是本地文件，两边张数一样就按先后。
  张数不一样、先后不一样、字有一成以上对不上、有图和字在同一行，都不挪，原因写进报告。
- 挪好的记 ``raw_meta.images = "page"``（和新抓的「- 图片位置：原文页」一样），阅读页不再按识图挪；
  作者写的字没变，观点、摘要的哈希跟过来，不重抽（ingest.store 认「只是图挪了位置」）。
  归档文件不改：以后整目录重新导入时，库里按原文页摆好的留着（同一处规则）。
- 原文页只下网页，不花钱。微信要验证、连着几篇取不到原文页就这次先停，那几篇下次再看；
  挪不了的记 ``raw_meta.images_checked``，这一版的规则（``LAYOUT_VERSION``）下不再取。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from researchlib.boilerplate import _is_archive_header_line
from researchlib.revisions import IMAGE_LINE, images_moved_only
from researchlib.textutil import SHANGHAI

# 规则改了就加一：以前挪不了的篇按新规则再看一遍。
LAYOUT_VERSION = 1
# 归档正文里的字至少有这么多在原文页上按顺序对得上，才按原文页挪。
COVERAGE = 0.9
# 对齐时短于这么多字的零碎对上不算，免得一个「的」把图拉到别处。
MIN_RUN = 4
# 字太多时 difflib 太慢；方伟公众号最长的也远没有这么长。
MAX_CHARS = 80_000
PAUSE_S = 2.0
# 连着这么多篇没取到原文页（连不上、页面认不出），这次先停。
MAX_NETWORK_FAILURES = 3

_ANY_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.I)
_MD_DEST = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
_HTML_SRC = re.compile(r"""\b(?:data-src|src)\s*=\s*["']([^"']+)["']""", re.I)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")
_KEEP = re.compile(r"[0-9a-z一-鿿]+")
_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_ORIGINAL_LINK = re.compile(r"(?m)^\s*[-*]?\s*原(?:文)?链接\s*[:：]\s*<?(https?://[^\s>`)]+)")
_ANY_WECHAT_LINK = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s>`)\]]*")
# 原文页上要验证：这次先停，别的篇也别再连。
_VERIFY = ("微信要求验证（环境异常）", "微信要求验证")


@dataclass
class Placement:
    body_md: str
    images: int = 0
    moved: int = 0
    reason: str = ""


def _chars(text: str) -> str:
    """只留字母、数字和汉字，用来对齐。链接留文字，HTML 标签不要。"""
    text = _LINK.sub(r"\1", text)
    text = _TAG.sub("", text)
    text = unicodedata.normalize("NFKC", unescape(text)).lower()
    return "".join(_KEEP.findall(text))


def _image_ref(markup: str) -> str:
    found = _MD_DEST.match(markup) or _HTML_SRC.search(markup)
    return unescape(found.group(1)).strip() if found else ""


def image_key(ref: str) -> str | None:
    """图床地址里图片的 id（mmbiz.qpic.cn/mmbiz_png/<id>/640 的 <id>）。本地文件返回 None。"""
    ref = (ref or "").strip()
    if ref.startswith("//"):
        ref = "https:" + ref
    if not ref.startswith(("http://", "https://")):
        return None
    parts = urlsplit(ref)
    host = (parts.hostname or "").lower()
    segments = [segment for segment in parts.path.split("/") if segment]
    if (host.endswith("qpic.cn") or host.endswith("qlogo.cn")) and len(segments) >= 2:
        return segments[1]
    return host + parts.path.rstrip("/")


def _head_length(lines: list[str]) -> int:
    """开头的 frontmatter、标题和归档头（作者、时间、sn、原链接……）有几行。图不挪到它们前面，它们也不参与对齐。

    标题只认最前面那一行 # 标题；归档头后面的小标题是正文，原文页上也有。
    """
    start = 0
    if lines and lines[0].strip() == "---":
        closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if closing is not None:
            start = closing + 1
    seen = False
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if _is_archive_header_line(line) or (not seen and _HEADING.match(line)):
            seen = True
            continue
        return index
    return len(lines)


def _pair(archive_refs: list[str], page_refs: list[str]) -> list[int] | str:
    """归档里第 i 张图是原文页上的第几张。对不上时返回原因。"""
    keys = [image_key(ref) for ref in archive_refs]
    if all(keys):
        page_keys = [image_key(ref) for ref in page_refs]
        pairs: list[int] = []
        used: set[int] = set()
        for key in keys:
            match = next((at for at, other in enumerate(page_keys) if other == key and at not in used), None)
            if match is None:
                break
            used.add(match)
            pairs.append(match)
        else:
            return pairs
    if len(archive_refs) == len(page_refs):
        return list(range(len(archive_refs)))
    return f"图的张数对不上（归档 {len(archive_refs)} 张，原文页 {len(page_refs)} 张）"


def _page_stream(page_md: str) -> tuple[str, list[tuple[int, str]]]:
    """原文页的字（只留字母、数字和汉字）和每张图前面有几个字。"""
    pieces: list[str] = []
    images: list[tuple[int, str]] = []
    length = 0
    for line in page_md.split("\n"):
        at = 0
        for match in _ANY_IMAGE.finditer(line):
            piece = _chars(line[at : match.start()])
            pieces.append(piece)
            length += len(piece)
            images.append((length, _image_ref(match.group(0))))
            at = match.end()
        piece = _chars(line[at:])
        pieces.append(piece)
        length += len(piece)
    return "".join(pieces), images


def _mapper(blocks: list[tuple[int, int, int]]) -> Callable[[int], int]:
    """原文页上第 p 个字的位置 → 归档正文里的位置。落在没对上的地方时取前面最后一个对上的字之后。"""

    def to_archive(position: int) -> int:
        last = 0
        for page_at, archive_at, size in blocks:
            if position < page_at:
                break
            if position <= page_at + size:
                return archive_at + (position - page_at)
            last = archive_at + size
        return last

    return to_archive


def place_like_page(body_md: str, page_md: str) -> Placement:
    """按原文页的图文顺序重排归档正文里单独成行的图。字和行都不改，只挪图所在的行。"""
    trailing = "\n" if body_md.endswith("\n") else ""
    lines = body_md.rstrip("\n").split("\n")
    head = _head_length(lines)
    image_rows = [index for index in range(head, len(lines)) if IMAGE_LINE.match(lines[index])]
    rows = set(image_rows)
    units: list[tuple[int, int, int]] = []  # (行号, 起, 止)：归档正文里有字的行，起止是在对齐用的字里的位置
    archive_parts: list[str] = []
    length = 0
    for index in range(head, len(lines)):
        line = lines[index]
        if index in rows or not line.strip():
            continue
        if _ANY_IMAGE.search(line):
            return Placement(body_md, len(image_rows), reason="有图和字在同一行")
        piece = _chars(line)
        units.append((index, length, length + len(piece)))
        archive_parts.append(piece)
        length += len(piece)
    if not image_rows:
        return Placement(body_md, reason="正文里没有图")
    archive_text = "".join(archive_parts)
    page_text, page_images = _page_stream(page_md)
    if len(archive_text) > MAX_CHARS or len(page_text) > MAX_CHARS:
        return Placement(body_md, len(image_rows), reason=f"正文太长（超过 {MAX_CHARS} 字）")
    # 一行几张图时每张都对，这一行按第一张的位置整体挪。
    per_row = [[_image_ref(match.group(0)) for match in _ANY_IMAGE.finditer(lines[index])] for index in image_rows]
    paired = _pair([ref for refs in per_row for ref in refs], [ref for _at, ref in page_images])
    if isinstance(paired, str):
        return Placement(body_md, len(image_rows), reason=paired)
    if any(later <= earlier for earlier, later in zip(paired, paired[1:], strict=False)):
        return Placement(body_md, len(image_rows), reason="图的先后和原文页不一样")
    blocks: list[tuple[int, int, int]] = []
    if archive_text and len(page_text) < COVERAGE * len(archive_text):
        # 原文页的字比归档少一成以上，怎么对也到不了，不用算。
        return Placement(body_md, len(image_rows), reason="原文页的字比归档里少一成以上")
    if archive_text:
        blocks = [
            (block.a, block.b, block.size)
            for block in SequenceMatcher(None, page_text, archive_text, autojunk=False).get_matching_blocks()
            if block.size >= MIN_RUN
        ]
        matched = sum(size for _a, _b, size in blocks)
        if matched < COVERAGE * len(archive_text):
            return Placement(
                body_md, len(image_rows), reason=f"字只有 {matched / len(archive_text):.0%} 对得上原文页"
            )
    to_archive = _mapper(blocks)
    # 边界 k：第 k 个有字的行前面（k = len(units) 是最后一行后面）在对齐用的字里的位置。
    bounds = [start for _index, start, _end in units] + [length]
    targets: dict[int, list[str]] = {}
    moved = 0
    first = 0
    for row, refs in zip(image_rows, per_row, strict=True):
        before = sum(1 for index, _start, _end in units if index < row)
        where = to_archive(page_images[paired[first]][0])
        first += len(refs)
        if bounds[before] == where:
            target = before
        else:
            target = min(range(len(bounds)), key=lambda k: (abs(bounds[k] - where), k))
        if target != before:
            moved += 1
        targets.setdefault(target, []).append(lines[row])
    if not moved:
        return Placement(body_md, len(image_rows))
    out = lines[:head]
    unit = 0
    unit_rows = {index for index, _start, _end in units}
    for index in range(head, len(lines)):
        if index in rows:
            continue
        if index in unit_rows:
            for image in targets.pop(unit, []):
                out.extend(["", image, ""])
            unit += 1
        out.append(lines[index])
    for image in targets.pop(unit, []):
        out.extend(["", image, ""])
    placed = re.sub(r"\n[ \t]*(?:\n[ \t]*){2,}", "\n\n", "\n".join(out)).strip("\n") + trailing
    if not images_moved_only(body_md, placed):
        return Placement(body_md, len(image_rows), reason="挪完和原来的字或图对不上（没有写入）")
    return Placement(placed, len(image_rows), moved)


# 库里的文章 ---------------------------------------------------------------------


def original_link(body_md: str, raw_meta: dict | None = None) -> str:
    """归档头「- 原链接：」，没有就取开头一段里公众号文章的地址。"""
    head = (body_md or "")[:3000]
    found = _ORIGINAL_LINK.search(head)
    url = found.group(1) if found else ""
    if not url:
        loose = _ANY_WECHAT_LINK.search(head)
        url = loose.group(0) if loose else str((raw_meta or {}).get("url") or "")
    url = unescape(url).strip().split("#", 1)[0]
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    return url if url.startswith("https://mp.weixin.qq.com/") else ""


@dataclass
class LayoutReport:
    source_id: str
    source_name: str
    waiting: int = 0
    placed: list[str] = field(default_factory=list)
    images_moved: int = 0
    in_place: int = 0
    unplaced: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    skipped: int = 0
    stopped: str = ""
    dry_run: bool = False

    @property
    def looked(self) -> int:
        return len(self.placed) + self.in_place + len(self.unplaced) + len(self.gone)

    def render(self) -> str:
        lines = [
            f"来源: {self.source_id}（{self.source_name}）",
            f"等着按原文页摆图的: {self.waiting} 篇，这次看了 {self.looked} 篇"
            + ("（--dry-run，不写库）" if self.dry_run else ""),
            f"按原文页挪好: {len(self.placed)} 篇（挪了 {self.images_moved} 张图）",
        ]
        lines.extend(f"  + {item}" for item in self.placed)
        lines.append(f"图本来就在原位: {self.in_place}")
        if self.unplaced:
            lines.append(f"挪不了，阅读页照旧按识图挪: {len(self.unplaced)}")
            lines.extend(f"  ! {item}" for item in self.unplaced)
        if self.gone:
            lines.append(f"原文已经没了: {len(self.gone)}")
            lines.extend(f"  - {item}" for item in self.gone)
        if self.skipped:
            lines.append(f"这次没取到原文页、下次再看: {self.skipped}")
        if self.stopped:
            lines.append(f"这次先停: {self.stopped}")
        return "\n".join(lines)


_WAITING = """
    FROM articles
    WHERE source_id = %s
      AND coalesce(raw_meta->>'images', '') <> 'page'
      AND coalesce((raw_meta->'images_checked'->>'version')::int, 0) < %s
      AND (body_md LIKE '%%![%%' OR body_md ILIKE '%%<img%%')
"""


def _label(row) -> str:
    day = row["published_at"].astimezone(SHANGHAI).date().isoformat() if row["published_at"] else "无日期"
    return f"{day} {row['title']}（#{row['id']}）"


def _mark(conn, article_id: int, reason: str, today: str) -> None:
    """这一版规则下挪不了：记下来，不再取原文页。只改 raw_meta，正文和哈希不动。"""
    from psycopg.types.json import Jsonb

    conn.execute(
        "UPDATE articles SET raw_meta = coalesce(raw_meta, '{}'::jsonb) || %s WHERE id = %s",
        (Jsonb({"images_checked": {"version": LAYOUT_VERSION, "on": today, "reason": reason}}), article_id),
    )
    conn.commit()


def _store_placed(conn, source, row, body_md: str, data_dir: Path, matcher) -> bool:
    """写回挪好的正文。取原文页这几秒里正文被别的导入改了，就不写，下次再看。"""
    from researchlib.ingest.model import ParsedDoc
    from researchlib.ingest.store import _store_one

    current = conn.execute("SELECT content_hash FROM articles WHERE id = %s FOR UPDATE", (row["id"],)).fetchone()
    if current is None or current["content_hash"] != row["content_hash"]:
        conn.rollback()
        return False
    meta = {key: value for key, value in (row["raw_meta"] or {}).items() if key != "images_checked"}
    meta["images"] = "page"
    doc = ParsedDoc(
        external_id=row["external_id"],
        title=row["title"],
        published_at=row["published_at"],
        body_md=body_md,
        raw_meta=meta,
        image_refs=[],
    )
    try:
        # 和导入走同一条路：只是图挪了位置，观点、摘要的哈希跟过来，检索块和提及按新正文重算。
        _store_one(conn, source, doc, data_dir, matcher)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def relayout(
    conn,
    source,
    *,
    data_dir: Path,
    fetch: Callable[[str], str],
    limit: int = 50,
    dry_run: bool = False,
    pause: float = PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> LayoutReport:
    from researchlib.mentions import CompanyMatcher, load_company_rows
    from researchlib.wechat_crawl import GONE
    from researchlib.wechat_html import page_markdown, unavailable_reason

    report = LayoutReport(source_id=source.id, source_name=source.name, dry_run=dry_run)
    report.waiting = int(
        conn.execute(f"SELECT count(*) AS n {_WAITING}", (source.id, LAYOUT_VERSION)).fetchone()["n"]
    )
    rows = conn.execute(
        f"""
        SELECT id, external_id, title, published_at, body_md, raw_meta, content_hash
        {_WAITING}
        ORDER BY published_at DESC NULLS LAST, id DESC
        LIMIT %s
        """,
        (source.id, LAYOUT_VERSION, max(limit, 0)),
    ).fetchall()
    conn.commit()
    today = datetime.now(SHANGHAI).date().isoformat()
    matcher = None
    failures = 0
    fetched = 0
    for row in rows:
        url = original_link(row["body_md"], row["raw_meta"])
        if not url:
            report.unplaced.append(f"{_label(row)}：归档里没有原文链接")
            if not dry_run:
                _mark(conn, row["id"], "归档里没有原文链接", today)
            continue
        if fetched and pause:
            sleep(pause)
        fetched += 1
        try:
            page = fetch(url)
        except (OSError, ValueError) as exc:
            page, why = "", f"原文页连不上：{getattr(exc, 'reason', exc)}"
        else:
            why = ""
        page_md = page_markdown(page)[0] if page else ""
        if not page_md:
            why = why or unavailable_reason(page)
            if why in _VERIFY:
                report.stopped = why
                break
            if why in GONE:
                report.gone.append(f"{_label(row)}：{why}")
                if not dry_run:
                    _mark(conn, row["id"], why, today)
                failures = 0
                continue
            # 连不上、认不出的页面：可能是一时的，不记，下次再看；连着几篇都这样就先停。
            report.skipped += 1
            failures += 1
            if failures >= MAX_NETWORK_FAILURES:
                report.stopped = f"连着 {failures} 篇没取到原文页（最后一篇：{why}）"
                break
            continue
        failures = 0
        placement = place_like_page(row["body_md"], page_md)
        if placement.reason:
            report.unplaced.append(f"{_label(row)}：{placement.reason}")
            if not dry_run:
                _mark(conn, row["id"], placement.reason, today)
            continue
        if not placement.moved:
            report.in_place += 1
            if not dry_run:
                conn.execute(
                    "UPDATE articles SET raw_meta = (coalesce(raw_meta, '{}'::jsonb) - 'images_checked')"
                    " || '{\"images\": \"page\"}'::jsonb WHERE id = %s",
                    (row["id"],),
                )
                conn.commit()
            continue
        if not dry_run:
            if matcher is None:
                matcher = CompanyMatcher(load_company_rows(conn))
            if not _store_placed(conn, source, row, placement.body_md, data_dir, matcher):
                report.skipped += 1
                continue
        report.placed.append(f"{_label(row)}：{placement.images} 行图挪了 {placement.moved} 行")
        report.images_moved += placement.moved
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="以前入库的公众号文章：图按公众号原文页的位置摆，字不改（只下网页，不花钱）"
    )
    parser.add_argument("--source", required=True, help="kind 是 wechat_mp 的来源（方伟公众号）")
    parser.add_argument("--limit", type=int, default=50, help="这次最多看几篇（默认 50，新的先看）")
    parser.add_argument("--pause", type=float, default=PAUSE_S, help=f"两次取原文页之间隔几秒（默认 {PAUSE_S:g}）")
    parser.add_argument("--dry-run", action="store_true", help="照样取原文页、算怎么挪，不写库")
    args = parser.parse_args(argv)

    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.sources import SourceRejected, load_sources, require_source
    from researchlib.wechat_crawl import fetch_page

    settings = Settings.from_env()
    try:
        source = require_source(load_sources(settings.sources_path), args.source)
    except SourceRejected as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2
    if source.kind != "wechat_mp":
        print(f"拒绝：{source.id} 的类型是 {source.kind}；只有 wechat_mp 的图位置是估的", file=sys.stderr)
        return 2
    conn = prepare(settings)
    try:
        report = relayout(
            conn,
            source,
            data_dir=settings.data_dir,
            fetch=fetch_page,
            limit=args.limit,
            dry_run=args.dry_run,
            pause=max(args.pause, 0.0),
        )
    finally:
        conn.close()
    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())

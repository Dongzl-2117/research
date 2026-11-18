"""Stratechery 每天抓一次：读订阅自带的个人 RSS 全文源，库里没有的写进归档再导入。

以前没有定时抓取，靠另一台机器「盒子」手动抓再导入（文章头里写着 ``- source: member-rss``），
10-18 以后没人跑，线上停在 10-15。这里照那份归档的写法：

- 文件 ``data/inbox/stratechery/<年>/<slug>.md``，YAML 头 ``id`` 是 ``<年>/<slug>``（取自原文链接
  ``https://stratechery.com/<年>/<slug>/``），和库里已有的 ``external_id`` 一致，同一篇再抓只会更新、不重复。
- ``published`` 写 RSS 给的 UTC 时刻（带钟点，不用等夜间按官网接口补）；碰上跨年
  （上海时间已经是下一年、链接还是上一年）按官网所属的美东年份核对，保留原来的钟点。
- 正文是 RSS 里 ``content:encoded`` 的 HTML，按公众号那套转换转成 Markdown（段落、标题、列表、引用、
  链接、加粗、图片地址），图片由 ``research-fetch-images`` 下载。

导入新写的文件，并补交本来源已写好、上次尚未入库的 RSS 归档。
个人 RSS 地址是订阅凭证：只从环境变量 ``STRATECHERY_RSS_URL`` 读（Mac mini 仓库根 ``.env``，
docker compose 透传进容器），不打印、不写进日志和报错。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from researchlib.ingest.store import body_chars
from researchlib.textutil import split_frontmatter
from researchlib.visibility import SHORT_POST
from researchlib.wechat_html import html_to_markdown

SOURCE_ID = "stratechery"
ENV_URL = "STRATECHERY_RSS_URL"
EASTERN = ZoneInfo("America/New_York")
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
# 只收正文文章：https://stratechery.com/2026/some-slug/。播客、视频页、站外链接不收。
_ARTICLE_URL = re.compile(r"^https?://(?:www\.)?stratechery\.com/(\d{4})/([a-z0-9][a-z0-9-]*)/?(?:[?#].*)?$", re.I)
_WEEKLY_PREFIX = re.compile(r"^\s*\d{4}\.\d{1,2}:\s*")
USER_AGENT = "research-archive/1.0 (+stratechery member feed)"


@dataclass
class Item:
    title: str
    url: str
    year: str
    slug: str
    published: datetime | None
    html: str
    categories: list[str] = field(default_factory=list)

    @property
    def external_id(self) -> str:
        return f"{self.year}/{self.slug}"


@dataclass
class CrawlReport:
    listed: int = 0
    skipped: int = 0
    known: int = 0
    written: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    dry_run: bool = False

    def render(self) -> str:
        lines = [
            f"Stratechery RSS：列表 {self.listed} 条，不是正文文章 {self.skipped} 条，库里已有 {self.known} 篇，"
            + (f"新的 {len(self.written)} 篇（只看不写）" if self.dry_run else f"新写 {len(self.written)} 篇")
        ]
        lines += [f"  新：{line}" for line in self.written]
        lines += [f"  失败：{line}" for line in self.failed]
        return "\n".join(lines)


class FeedError(RuntimeError):
    pass


def fetch_feed(url: str, timeout: int = 60) -> bytes:
    """取 RSS。报错里不带地址（地址里有订阅令牌）。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, */*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 固定的 https 地址
            return response.read()
    except urllib.error.HTTPError as exc:
        hint = "：地址失效或订阅过期，到 Stratechery 账户页重新复制 RSS 地址" if exc.code in (401, 403, 404) else ""
        raise FeedError(f"RSS 回 HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise FeedError(f"RSS 连不上：{getattr(exc, 'reason', exc)}") from None


def _published(text: str | None) -> datetime | None:
    if not text or not text.strip():
        return None
    try:
        moment = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_feed(data: bytes) -> tuple[list[Item], int, list[Item]]:
    """RSS → (正文文章, 跳过的条数, 认不出地址的正文)。整份不是 RSS 时抛 FeedError。

    11-18 实测个人 RSS 50 条里有 20 条正文齐全、``link`` 却只写了首页 ``https://stratechery.com/``
    （Daily Update、访谈、每周汇总都有）。先看 ``guid``；还认不出的放进第三项，由 ``resolve_by_title``
    按官网接口用标题和发布时刻认回原文地址。
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"RSS 不是合法的 XML（{exc}）：可能被重定向到了登录页") from None
    channel = root.find("channel")
    if channel is None:
        raise FeedError("RSS 里没有 channel：可能被重定向到了登录页")
    items: list[Item] = []
    unresolved: list[Item] = []
    skipped = 0
    for node in channel.findall("item"):
        html = node.findtext(CONTENT_NS) or ""
        title = (node.findtext("title") or "").strip()
        if not html.strip():
            skipped += 1
            continue
        link = (node.findtext("link") or "").strip()
        match = _ARTICLE_URL.fullmatch(link)
        if not match and _on_site(link):
            match = _ARTICLE_URL.fullmatch((node.findtext("guid") or "").strip())
        item = Item(
            title=title,
            url=f"https://stratechery.com/{match.group(1)}/{match.group(2).lower()}/" if match else "",
            year=match.group(1) if match else "",
            slug=match.group(2).lower() if match else "",
            published=_published(node.findtext("pubDate")),
            html=html,
            categories=[(c.text or "").strip() for c in node.findall("category") if (c.text or "").strip()],
        )
        if match:
            items.append(item)
        elif _on_site(link):
            unresolved.append(item)
        else:
            skipped += 1
    return items, skipped, unresolved


def _on_site(link: str) -> bool:
    """链接只写了 Stratechery 首页（或干脆没写）的才去官网认；播客、站外链接照旧跳过。"""
    link = link.strip()
    if link in ("", "/"):
        return True
    try:
        url = urlsplit(link)
    except ValueError:
        return False
    return url.netloc.lower() in ("stratechery.com", "www.stratechery.com") and url.path in ("", "/")


def resolve_by_title(unresolved: list[Item], *, get=None) -> tuple[list[Item], list[str]]:
    """按官网接口认回地址：标题（去掉实体、标点后）一样、发布时刻差不到一天的唯一一篇。

    返回 (认出来的, 认不出的说明)。官网接口连不上时整批算认不出，下一晚再试。
    """
    if not unresolved:
        return [], []
    from researchlib.stratechery_times import fetch_posts, title_key

    problems = [
        f"{item.title or '无标题条目'}：首页链接缺少可比对的标题或发布时间"
        for item in unresolved if not title_key(item.title) or item.published is None
    ]
    candidates = [item for item in unresolved if title_key(item.title) and item.published is not None]
    if not candidates:
        return [], problems
    days = [item.published.astimezone(EASTERN).date() for item in candidates]
    kwargs = {"get": get} if get is not None else {}
    try:
        posts = fetch_posts(min(days), max(days), **kwargs)
    except Exception as exc:  # noqa: BLE001 官网接口出错不连累认得出地址的那些
        problems.extend(f"{item.title}：官网接口连不上，认不出地址（{type(exc).__name__}）" for item in candidates)
        return [], problems
    resolved: list[Item] = []
    for item in candidates:
        key = title_key(item.title)
        found = []
        for post in posts:
            title = post.get("title")
            title = title.get("rendered", "") if isinstance(title, dict) else str(title or "")
            when = _published_gmt(post.get("date_gmt"))
            # 每周汇总在官网叫「2026.46: The End of Jensen」，RSS 里只有冒号后面那半。
            if (
                key not in (title_key(title), title_key(_WEEKLY_PREFIX.sub("", html_unescape(title))))
                or when is None
                or abs(when - item.published).total_seconds() >= 86400
            ):
                continue
            slug = str(post.get("slug") or "").lower()
            local = str(post.get("date") or "")
            if re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) and re.match(r"\d{4}-", local):
                found.append((local[:4], slug))
        if len(set(found)) != 1:
            why = "官网上没找到同名同日的文章" if not found else "官网上同名同日的有好几篇"
            problems.append(f"{item.title}：RSS 里没给原文地址，{why}")
            continue
        year, slug = found[0]
        item.year, item.slug, item.url = year, slug, f"https://stratechery.com/{year}/{slug}/"
        resolved.append(item)
    return resolved, problems


def _published_gmt(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def archive_state(conn, source_id: str) -> tuple[set[str], set[str]]:
    rows = conn.execute(
        "SELECT external_id, raw_meta->>'hidden' AS hidden FROM articles WHERE source_id = %s", (source_id,),
    ).fetchall()
    return {row["external_id"] for row in rows}, {row["external_id"] for row in rows if row["hidden"] == SHORT_POST}


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def published_value(item: Item) -> str:
    """RSS 提供精确时刻时，写入时区明确的 UTC。"""
    return item.published.astimezone(UTC).isoformat() if item.published is not None else ""


def archive_markdown(item: Item, body: str, author: str) -> str:
    when = published_value(item)
    kind = "interview" if item.title.lower().startswith(("an interview", "a conversation")) else "article"
    head = [
        "---",
        f"id: {_quote(item.external_id)}",
        f"title: {_quote(item.title)}",
        f"url: {_quote(item.url)}",
    ]
    if when:
        # 导入先认 date：两行都写带钟点的时刻，不然钟点被日历日盖掉。
        head += [f"published: {_quote(when)}", f"date: {_quote(when)}", "clock_source: rss_pub_date"]
    head += [f"author: {_quote(author)}", f"type: {_quote(kind)}", "---", ""]
    return "\n".join(head) + f"\n# {item.title}\n\n- source: member-rss\n\n" + body.strip() + "\n"


def archive_path(inbox: Path, item: Item) -> Path:
    return inbox / item.year / f"{item.slug}.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


def member_archive(path: Path, key: str) -> str:
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    url = f"https://stratechery.com/{key}/"
    if meta.get("id") == key and meta.get("url") == url and "- source: member-rss" in body.split("\n", 10)[:10]:
        return text
    return ""


def crawl(
    items: list[Item], known: set[str], *, inbox: Path, author: str, dry_run: bool = False, max_new: int = 50,
    short_ids: set[str] | None = None, min_chars: int = 0,
) -> tuple[CrawlReport, list[Path]]:
    report = CrawlReport(listed=len(items), dry_run=dry_run)
    written: list[Path] = []
    groups: dict[str, list[Item]] = {}
    for item in items:
        groups.setdefault(item.external_id, []).append(item)
    for external_id, copies in groups.items():
        upgrade = external_id in (short_ids or ())
        if external_id in known and not upgrade:
            report.known += len(copies)
            continue
        # 同一 RSS 偶尔既有预览又有全文；选转换后正文最全的一份，空壳不占用 ID。
        best_size = 0
        best_text = ""
        label = f"{external_id}（{copies[0].title}）"
        problem = "RSS 里的正文是空的"
        for item in copies:
            try:
                body, _images = html_to_markdown(item.html)
            except Exception as exc:  # noqa: BLE001 一份转坏不连累另一份
                problem = f"正文转换出错（{type(exc).__name__}）"
                continue
            if body.strip():
                text = archive_markdown(item, body, author)
                size = body_chars(text)
                if size > best_size:
                    best_text, best_size = text, size
        if not best_text:
            report.failed.append(f"{label}：{problem}")
            continue
        if upgrade and best_size < min_chars:
            report.known += len(copies)
            continue
        if len(report.written) >= max_new:
            report.failed.append(f"{label}：这次已写满 {max_new} 篇，下次再取")
            continue
        if not dry_run:
            path = archive_path(inbox, copies[0])
            try:
                # 昨日可能已写下全文、导入却中断。RSS 当天若退回预览，仍导入更完整的落盘版。
                previous = member_archive(path, external_id) if path.is_file() else ""
                if not previous or body_chars(previous) < best_size:
                    _write(path, best_text)
            except (OSError, ValueError) as exc:
                report.failed.append(f"{label}：归档写入失败（{type(exc).__name__}）")
                continue
            written.append(path)
        report.written.append(label)
    return report, written


def pending_archives(inbox: Path, known: set[str], short_ids: set[str], written: list[Path],
                     min_chars: int, limit: int, problems: list[str]) -> list[Path]:
    """从来源自己的归档补交已经滚出 RSS 的文件，只认规范 ID、原文地址和会员源标记。"""
    pending = []
    fresh = set(written)
    for path in sorted(inbox.glob("[0-9][0-9][0-9][0-9]/*.md")):
        if len(pending) >= limit:
            break
        key = path.relative_to(inbox).with_suffix("").as_posix()
        if path in fresh or (key in known and key not in short_ids):
            continue
        url = f"https://stratechery.com/{key}/"
        if not _ARTICLE_URL.fullmatch(url):
            continue
        try:
            if key in short_ids:
                info = path.stat()
                # body_chars 只从 UTF-8 原文删字。已知短帖连文件字节数都不够，无法补成全文。
                if stat.S_ISREG(info.st_mode) and info.st_size < min_chars and os.access(path, os.R_OK):
                    continue
            text = member_archive(path, key)
        except (OSError, ValueError) as exc:
            problems.append(f"{key}：待导入归档读取失败（{type(exc).__name__}）")
            continue
        if text and (key not in short_ids or body_chars(text) >= min_chars):
            pending.append(path)
    return pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stratechery 定时抓取：读个人 RSS 全文源，库里没有的写归档再导入")
    parser.add_argument("--dry-run", action="store_true", help="只读 RSS、说哪些是新的，不写不导入")
    parser.add_argument("--no-import", action="store_true", help="只写归档，不导入")
    parser.add_argument("--max-new", type=int, default=50, help="这次最多写几篇（默认 50）")
    args = parser.parse_args(argv)

    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.ingest.cli import run_markdown_ingest
    from researchlib.sources import SourceRejected, load_sources, require_source

    settings = Settings.from_env()
    try:
        source = require_source(load_sources(settings.sources_path), SOURCE_ID)
    except SourceRejected as exc:
        print(f"拒绝抓取：{exc}", file=sys.stderr)
        return 2
    url = os.environ.get(ENV_URL, "").strip()
    if not url:
        print(f"缺少 {ENV_URL}（仓库根 .env，docker compose 透传进容器）", file=sys.stderr)
        return 2
    if not url.lower().startswith("https://"):
        print(f"{ENV_URL} 要是 https 地址", file=sys.stderr)
        return 2
    try:
        items, skipped, unresolved = parse_feed(fetch_feed(url))
        listed = len(items) + skipped + len(unresolved)
        found, problems = resolve_by_title(unresolved)
        items += found
        if not items and not problems:
            problems.append(f"RSS 里一篇正文文章都没有（跳过 {skipped} 条），请核对订阅源")
    except FeedError as exc:
        items, skipped, listed, problems = [], 0, 0, [str(exc)]
    inbox = settings.data_dir / "inbox" / source.id
    conn = prepare(settings)
    try:
        known, short_ids = archive_state(conn, source.id)
    finally:
        conn.close()
    limit = max(args.max_new, 0)
    report, written = crawl(
        items, known, inbox=inbox, author=source.author, dry_run=args.dry_run, max_new=limit,
        short_ids=short_ids, min_chars=source.min_chars,
    )
    report.listed = listed
    report.skipped = skipped
    report.failed += problems
    recovered = [] if args.no_import else pending_archives(
        inbox, known, short_ids, written, source.min_chars, limit, report.failed,
    )
    print(report.render())
    if recovered:
        print(f"上次已归档、待补导入 {len(recovered)} 篇")
    if args.dry_run or args.no_import or not (written or recovered):
        return 1 if report.failed else 0
    with tempfile.TemporaryDirectory(prefix="stratechery-rss-") as tmp:
        for path in [*recovered, *written]:
            target = Path(tmp) / path.relative_to(inbox)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        imported = run_markdown_ingest(
            "markdown",
            "导入抓下来的 Stratechery",
            ["--source", source.id, "--input", tmp, "--origin", "crawl"],
            id_mode="path",
        )
    return 1 if report.failed or imported else 0


if __name__ == "__main__":
    sys.exit(main())

"""公众号定时抓取：极致了（dajiala）查最新文章，新文章的正文从公众号原文页取，写成归档 Markdown 再导入。

    docker compose exec app research-crawl-wechat --source gudujinhuati
    docker compose exec app research-crawl-wechat --source gudujinhuati --dry-run   # 只看哪些是新的

- 来源在 config/sources.yaml 里写 ``ghid``（公众号原始 ID，gh_ 开头）。key 是环境变量 ``DAJIALA_KEY``，
  和方伟公众号的抓取共用（仓库根 .env，docker compose 透传进容器）。
- 极致了按次收费：查一次最新文章列表约 0.14 元（--dry-run 也要查），取一篇正文约 0.03 元。
  正文先从原文页取（不花钱，图片位置也是对的），原文页取不到（要验证、图文消息之类）才用极致了的正文。
- 库里已有的（按 sn，或同一天同标题）跳过，一次最多取 --max-new 篇新文章。原文已删除、因违规不能看的，
  记一份原始数据就不再取。
- 归档写在 DATA_DIR/inbox/<来源>/：md/<年>/<日期>-<sn>.md、raw/articles/<sn>.json，格式和以前导入的公众号归档一样
  （正文开头「- 作者：」「- 时间：」「- sn：」「- 原链接：」）。图片留图床地址，入库后由 research-fetch-images 下载。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from researchlib.textutil import SHANGHAI
from researchlib.wechat_html import dajiala_markdown, page_markdown, unavailable_reason

DAJIALA_BASE = "https://www.dajiala.com"
# 极致了限速每分钟 5 次：两次调用之间至少隔这么久。
DAJIALA_PAUSE_S = 13.0
TIMEOUT_S = 30
MAX_PAGE_BYTES = 8 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
# 这些原因说明原文已经没了，以后也取不到：记下来不再取，也不算失败。
GONE = frozenset({"原文已删除", "原文因违规无法查看", "原文被投诉，无法查看"})
_SN = re.compile(r"sn(?:=|\s*[：:]\s*`?)([0-9a-f]{32})")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class DajialaError(Exception):
    pass


@dataclass
class Post:
    url: str
    title: str
    post_time: int
    sn: str
    raw: dict


def _sn_of(url: str) -> str:
    values = parse_qs(urlsplit(url).query).get("sn") or [""]
    return values[0].strip().lower()


def _clean_url(url: str) -> str:
    url = (url or "").strip().split("#", 1)[0]
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    return url


def parse_posts(data: object) -> list[Post]:
    """post_history 的 data：[{url, title, post_time, sn, …}]，新的在前。"""
    posts: list[Post] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        url = _clean_url(str(item.get("url") or ""))
        title = unescape(str(item.get("title") or "")).strip()
        if not url or not title:
            continue
        sn = str(item.get("sn") or "").strip().lower() or _sn_of(url)
        try:
            post_time = int(item.get("post_time") or 0)
        except (TypeError, ValueError):
            post_time = 0
        posts.append(Post(url=url, title=title, post_time=post_time, sn=sn, raw=item))
    posts.sort(key=lambda post: post.post_time, reverse=True)
    return posts


class Dajiala:
    """极致了的两个接口。出错时的说明不带 key。"""

    def __init__(self, key: str, base_url: str = DAJIALA_BASE, *, pause: float = DAJIALA_PAUSE_S) -> None:
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.pause = pause
        self.cost = 0.0
        self.calls = 0
        self._last = 0.0

    def _wait(self) -> None:
        if self.calls and self.pause:
            left = self.pause - (time.monotonic() - self._last)
            if left > 0:
                time.sleep(left)

    def _call(self, label: str, request: urllib.request.Request) -> dict:
        self._wait()
        self.calls += 1
        self._last = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                text = response.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise DajialaError(f"极致了 {label}：HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise DajialaError(f"极致了 {label} 连不上：{reason}") from None
        try:
            data = json.loads(text)
        except ValueError:
            raise DajialaError(f"极致了 {label} 返回的不是 JSON：{text[:80]}") from None
        if not isinstance(data, dict):
            raise DajialaError(f"极致了 {label} 返回的格式不对")
        cost = data.get("cost_money")
        if isinstance(cost, (int, float)):
            self.cost += float(cost)
        code = data.get("code", 0)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = -2
        if code == 20001:
            raise DajialaError(f"极致了余额不足（{label}）：{data.get('msg') or ''}".rstrip("："))
        if code == -1:
            raise DajialaError(f"极致了限速了（{label}），下次再试")
        if code != 0:
            raise DajialaError(f"极致了 {label} 返回 {code}：{data.get('msg') or ''}".rstrip("："))
        return data

    def history(self, ghid: str) -> tuple[list[Post], dict]:
        body = json.dumps({"ghid": ghid, "key": self.key, "verifycode": ""}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/fbmain/monitor/v3/post_history",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        data = self._call("post_history", request)
        meta = {
            "nickname": data.get("nickname"),
            "remain_money": data.get("remain_money"),
        }
        return parse_posts(data.get("data")), meta

    def article(self, url: str) -> dict:
        query = urlencode({"url": url, "key": self.key, "mode": "1", "verifycode": ""})
        request = urllib.request.Request(
            f"{self.base_url}/fbmain/monitor/v3/article_detail?{query}",
            headers={"Accept": "application/json"},
        )
        return self._call("article_detail", request)


def fetch_page(url: str) -> str:
    """公众号原文页。连不上抛 OSError。"""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return response.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")


def _title_key(title: str) -> str:
    return re.sub(r"\s+", "", title or "")


def _day(moment: datetime | None) -> str:
    return moment.astimezone(SHANGHAI).date().isoformat() if moment else ""


def known_articles(conn, source_id: str) -> tuple[set[str], set[tuple[str, str]]]:
    """库里这个来源已有的文章：sn（正文开头的归档头或原链接里），和（标题, 日期）。"""
    rows = conn.execute(
        """
        SELECT title, published_at, left(body_md, 4000) AS head, raw_meta::text AS meta
        FROM articles WHERE source_id = %s
        """,
        (source_id,),
    ).fetchall()
    sns: set[str] = set()
    titles: set[tuple[str, str]] = set()
    for row in rows:
        sns.update(_SN.findall(f"{row['head'] or ''}\n{row['meta'] or ''}"))
        titles.add((_title_key(row["title"]), _day(row["published_at"])))
    return sns, titles


def archived_sns(inbox: Path) -> set[str]:
    """归档目录里已经写过的（导入失败也算，导入那一步会再试）。"""
    folder = inbox / "raw" / "articles"
    if not folder.is_dir():
        return set()
    return {path.stem for path in folder.glob("*.json") if _HEX32.match(path.stem)}


def published(post: Post) -> datetime | None:
    return datetime.fromtimestamp(post.post_time, UTC).astimezone(SHANGHAI) if post.post_time > 0 else None


def archive_markdown(post: Post, body: str, author: str) -> str:
    when = published(post)
    lines = [f"# {post.title}", "", f"- 作者：{author}"]
    if when is not None:
        lines.append(f"- 时间：{when.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    lines.append(f"- sn：`{post.sn}`")
    lines.append(f"- 原链接：{post.url}")
    lines.append(f"- 原始数据：[raw/articles/{post.sn}.json](../../raw/articles/{post.sn}.json)")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def archive_path(inbox: Path, post: Post) -> Path:
    when = published(post)
    day = when.date().isoformat() if when else "0000-00-00"
    return inbox / "md" / day[:4] / f"{day}-{post.sn}.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)


@dataclass
class CrawlReport:
    source_id: str
    source_name: str
    listed: int = 0
    known: int = 0
    written: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    later: int = 0
    cost: float = 0.0
    remain: object = None
    dry_run: bool = False

    def render(self) -> str:
        money = f"花费 {self.cost:.2f} 元"
        if isinstance(self.remain, (int, float)):
            money += f"，余额 {self.remain:.2f} 元"
        lines = [
            f"来源: {self.source_id}（{self.source_name}）",
            f"极致了列表: {self.listed} 篇（{money}）",
            f"已在库里: {self.known}",
        ]
        if self.dry_run:
            lines.append(f"新文章: {len(self.new)}（--dry-run，没取正文、没写归档）")
            lines.extend(f"  + {item}" for item in self.new)
            return "\n".join(lines)
        lines.append(f"新写入归档: {len(self.written)}")
        lines.extend(f"  + {item}" for item in self.written)
        if self.gone:
            lines.append(f"原文已经没了，不再取: {len(self.gone)}")
            lines.extend(f"  - {item}" for item in self.gone)
        if self.later:
            lines.append(f"超过这次的上限，留到下次: {self.later}")
        if self.failed:
            lines.append(f"失败: {len(self.failed)}")
            lines.extend(f"  ! {item}" for item in self.failed)
        return "\n".join(lines)


def _label(post: Post) -> str:
    return f"{_day(published(post)) or '无日期'} {post.title}"


def _raw_record(post: Post, *, body_from: str, unavailable: str = "", detail: dict | None = None) -> dict:
    record = {
        "post": post.raw,
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "body_from": body_from,
    }
    if unavailable:
        record["unavailable"] = unavailable
    if detail:
        record["dajiala_article"] = {
            key: detail.get(key) for key in ("title", "author", "desc", "pubtime", "cost_money") if key in detail
        }
    return record


def crawl(
    conn,
    source,
    *,
    ghid: str,
    inbox: Path,
    client: Dajiala,
    fetch=fetch_page,
    max_new: int = 5,
    dry_run: bool = False,
) -> CrawlReport:
    report = CrawlReport(source_id=source.id, source_name=source.name, dry_run=dry_run)
    posts, meta = client.history(ghid)
    report.remain = meta.get("remain_money")
    report.listed = len(posts)
    sns, titles = known_articles(conn, source.id)
    sns |= archived_sns(inbox)
    for post in posts:
        if not _HEX32.match(post.sn):
            report.failed.append(f"{_label(post)}：列表里没有 sn，认不出是哪篇")
            continue
        if post.sn in sns or (_title_key(post.title), _day(published(post))) in titles:
            report.known += 1
            continue
        if dry_run:
            report.new.append(_label(post))
            continue
        if len(report.written) + len(report.failed) >= max_new:
            report.later += 1
            continue
        sns.add(post.sn)
        raw_path = inbox / "raw" / "articles" / f"{post.sn}.json"
        body, images, body_from, unavailable, detail = "", [], "", "", None
        try:
            page = fetch(post.url)
        except (OSError, ValueError) as exc:
            page, unavailable = "", f"原文页连不上：{getattr(exc, 'reason', exc)}"
        if page:
            body, images = page_markdown(page)
            if body:
                body_from = "page"
            else:
                unavailable = unavailable_reason(page)
        if not body and unavailable in GONE:
            _write(raw_path, json.dumps(_raw_record(post, body_from="", unavailable=unavailable), ensure_ascii=False))
            report.gone.append(f"{_label(post)}：{unavailable}")
            continue
        if not body:
            try:
                detail = client.article(post.url)
            except DajialaError as exc:
                report.failed.append(f"{_label(post)}：{unavailable}；{exc}")
                continue
            body, images = dajiala_markdown(str(detail.get("content") or ""))
            body_from = "dajiala"
            if not body:
                report.failed.append(f"{_label(post)}：{unavailable}；极致了的正文也是空的")
                continue
        _write(raw_path, json.dumps(_raw_record(post, body_from=body_from, detail=detail), ensure_ascii=False))
        _write(archive_path(inbox, post), archive_markdown(post, body, source.author))
        where = "原文页" if body_from == "page" else "极致了正文，图片位置是估的"
        report.written.append(f"{_label(post)}（{where}，{len(images)} 张图）")
    report.cost = client.cost
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公众号定时抓取：极致了查最新文章，原文页取正文，写归档再导入")
    parser.add_argument("--source", required=True, help="config/sources.yaml 里写了 ghid 的来源")
    parser.add_argument("--max-new", type=int, default=5, help="这次最多取几篇新文章（默认 5）")
    parser.add_argument("--dry-run", action="store_true", help="只查列表、说哪些是新的（列表照样收费）")
    parser.add_argument("--no-import", action="store_true", help="只写归档，不导入")
    args = parser.parse_args(argv)

    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.ingest.cli import run_markdown_ingest
    from researchlib.sources import SourceRejected, load_sources, require_source

    settings = Settings.from_env()
    try:
        source = require_source(load_sources(settings.sources_path), args.source)
    except SourceRejected as exc:
        print(f"拒绝抓取：{exc}", file=sys.stderr)
        return 2
    if source.kind not in ("markdown", "wechat_mp"):
        print(f"拒绝抓取：{source.id} 的类型是 {source.kind}，只抓公众号（markdown 或 wechat_mp）", file=sys.stderr)
        return 2
    if not source.ghid:
        print(f"拒绝抓取：{source.id} 在 config/sources.yaml 里没写 ghid（公众号原始 ID）", file=sys.stderr)
        return 2
    key = os.environ.get("DAJIALA_KEY", "").strip()
    if not key:
        print("缺少 DAJIALA_KEY（仓库根 .env，docker compose 透传进容器）", file=sys.stderr)
        return 2
    client = Dajiala(key, os.environ.get("DAJIALA_BASE_URL", "").strip() or DAJIALA_BASE)
    inbox = settings.data_dir / "inbox" / source.id
    conn = prepare(settings)
    try:
        report = crawl(
            conn,
            source,
            ghid=source.ghid,
            inbox=inbox,
            client=client,
            fetch=fetch_page,
            max_new=max(args.max_new, 0),
            dry_run=args.dry_run,
        )
    except DajialaError as exc:
        print(f"抓取失败：{exc}（这次花费 {client.cost:.2f} 元）", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(report.render())
    if args.dry_run or args.no_import or not inbox.is_dir():
        return 1 if report.failed else 0
    kind = "wechat_mp" if source.kind == "wechat_mp" else "markdown"
    imported = run_markdown_ingest(
        kind,
        "导入抓下来的公众号归档",
        ["--source", source.id, "--input", str(inbox), "--origin", "crawl"],
        id_mode="path",
    )
    return 1 if report.failed or imported else 0


if __name__ == "__main__":
    sys.exit(main())

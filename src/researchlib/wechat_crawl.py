"""公众号定时抓取：极致了（dajiala）查最新文章，新文章的正文从公众号原文页取，写成归档 Markdown 再导入。

    docker compose exec app research-crawl-wechat --source gudujinhuati
    docker compose exec app research-crawl-wechat --source fangwei-mp --dry-run   # 只看哪些是新的
    docker compose exec app research-crawl-wechat --source latepost --list-only --max-pages 3
    docker compose exec app research-crawl-wechat --source latepost --backfill --no-import

- 来源在 config/sources.yaml 里写 ``ghid``（公众号原始 ID，gh_ 开头）。
  不带 ``--backfill`` / ``--list-only`` 时只查最新一页，方伟公众号、孤独进化体、晚点LatePost 的定时任务都是这一条。
  key 是环境变量 ``DAJIALA_KEY``（仓库根 .env，docker compose 透传进容器）。
- 极致了按次收费：查一次文章列表约 0.14 元（``--dry-run``、``--list-only`` 也要查），取一篇正文约 0.03 元。
  正文先从原文页取（不花钱，图片位置也是对的），原文页取不到（要验证、图文消息之类）才用极致了的正文。
  极致了的正文按段估图的位置（第 N 张图放在第 N 段后面），归档头不写「图片位置：原文页」。
- ``--backfill`` / ``--list-only`` 才翻页：首页不传 offset，之后把上一页返回的 offset 原样送回去，``is_end`` 为 1 就停。
  每成功一页就把进度写进 ``data/inbox/<来源>/raw/post-history-state.json``，中断再跑从下一页接着查，已记下的页不再扣费。
  ``--list-only`` 只把列表写进 ``post-history.jsonl``，不取正文、不连库。
- 库里已有的按 sn 跳过；同一天同标题只拿库里查不到 sn 的老文章兜底（作者同一天发两篇同名的，两篇都取）。
  列表是空的、或者每一项都缺地址和标题，记失败（多半是账号被封、接口格式改了或 ghid 写错），不当成「没有新文章」。
- 原文页只取 https://mp.weixin.qq.com/ 的地址，跳转到别的域名也不跟。
- 一次最多取 --max-new 篇新文章（日常默认 5；``--backfill`` 不写这个参数就不限）。
  原文已删除、因违规不能看的，记一份原始数据就不再取。
- 列表里有比库里最新一篇还早的文章，却一篇都对不上库里的：多半是 ghid 写成了别的号，这次一篇都不取。
- 归档写在 DATA_DIR/inbox/<来源>/：md/<年>/<日期>-<sn>.md、raw/articles/<sn>.json，格式和以前导入的公众号归档一样
  （正文开头「- 作者：」「- 时间：」「- sn：」「- 原链接：」，正文从原文页取的再加一行「- 图片位置：原文页」）。
  图片留图床地址，入库后由 research-fetch-images 下载。导入本身不调模型。
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
from datetime import UTC, date, datetime
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
# 极致了价目（他们的文档：post_history 按 ghid 0.14 元/次，article_detail 约 0.03 元/次）。
# 接口回了 cost_money 就以接口为准；没回才用这两个数估。
LIST_CALL_CNY = 0.14
ARTICLE_CALL_CNY = 0.03
LISTING_NAME = "post-history.jsonl"
STATE_NAME = "post-history-state.json"
# 纯视频、纯音乐没有可归档的正文。纯图片、纯文字、图文、转载留下。
_NON_ARTICLE_TYPES = frozenset({5, 7})
_DELETED_STATUS = frozenset({6, 7})
_PENDING_STATUS = frozenset({104, 105})


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
        except (OverflowError, TypeError, ValueError):
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

    def history(self, ghid: str, offset: str | None = None) -> tuple[list[Post], dict]:
        """查一页历史文章。``offset`` 为空不传这个字段（最新一页，和以前的请求体一样）。

        翻页时把上一页返回的 offset 原样送回去。``is_end`` 为 True 表示没有下一页。
        """
        payload = {"ghid": ghid, "key": self.key, "verifycode": ""}
        if offset:
            payload["offset"] = offset
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/fbmain/monitor/v3/post_history",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        data = self._call("post_history", request)
        items = data.get("data")
        raw_cost = data.get("cost_money")
        page_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) else None
        nickname = data.get("nickname")
        meta = {
            "nickname": nickname if isinstance(nickname, str) else "",
            "remain_money": data.get("remain_money"),
            "offset": _cursor(data.get("offset")),
            "is_end": _parse_is_end(data.get("is_end")),
            "page_cost": page_cost,
            "raw_count": len(items) if isinstance(items, list) else 0,
            "total_num": _optional_int(data.get("total_num")),
        }
        return parse_posts(items), meta

    def article(self, url: str) -> dict:
        query = urlencode({"url": url, "key": self.key, "mode": "1", "verifycode": ""})
        request = urllib.request.Request(
            f"{self.base_url}/fbmain/monitor/v3/article_detail?{query}",
            headers={"Accept": "application/json"},
        )
        return self._call("article_detail", request)


PAGE_PREFIX = "https://mp.weixin.qq.com/"


def page_allowed(url: str) -> bool:
    """只取公众号原文页：列表地址来自第三方接口，不能让它把抓取指到内网或别的站。"""
    parts = urlsplit(url or "")
    return parts.scheme == "https" and parts.hostname == "mp.weixin.qq.com" and url.startswith(PAGE_PREFIX)


class _PageRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not page_allowed(newurl):
            raise ValueError(f"原文页跳到了别的域名：{urlsplit(newurl).hostname or newurl[:60]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_page_opener = urllib.request.build_opener(_PageRedirects)


def fetch_page(url: str) -> str:
    """公众号原文页。连不上抛 OSError，地址不是公众号原文页抛 ValueError。"""
    if not page_allowed(url):
        raise ValueError("不是公众号原文页的地址")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    with _page_opener.open(request, timeout=TIMEOUT_S) as response:
        return response.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")


def _title_key(title: str) -> str:
    return re.sub(r"\s+", "", title or "")


def _day(moment: datetime | None) -> str:
    return moment.astimezone(SHANGHAI).date().isoformat() if moment else ""


def known_articles(conn, source_id: str) -> tuple[set[str], set[tuple[str, str]]]:
    """库里这个来源已有的文章：sn（正文开头的归档头或原链接里），和查不到 sn 的老文章的（标题, 日期）。

    同一天同标题只给没有 sn 的老文章兜底：作者同一天发两篇同名文章（长文和短图文版）时 sn 不同，两篇都要取。
    """
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
        found = _SN.findall(f"{row['head'] or ''}\n{row['meta'] or ''}")
        if found:
            sns.update(found)
        else:
            titles.add((_title_key(row["title"]), _day(row["published_at"])))
    return sns, titles


def newest_article(conn, source_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT max(published_at) AS newest FROM articles WHERE source_id = %s",
        (source_id,),
    ).fetchone()
    return row["newest"] if row else None


def wrong_account(posts: list[Post], known: int, newest: datetime | None) -> bool:
    """列表里有比库里最新一篇还早的文章，却一篇都对不上：抓的多半不是这个号（ghid 写错了）。

    列表里的文章都比库里新的，是很久没抓、新文章超过一页，照常取。
    """
    if known or newest is None:
        return False
    return any((published(post) or newest) < newest for post in posts)


def archived_post(inbox: Path, post: Post) -> bool:
    """只认有正文的完整归档，或有明确下架原因的终态。两次写文件之间中断可以续抓。"""
    record = inbox / "raw" / "articles" / f"{post.sn}.json"
    if not record.is_file():
        return False
    if archive_path(inbox, post).is_file():
        return True
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    reason = data.get("unavailable") if isinstance(data, dict) else None
    return isinstance(reason, str) and reason in GONE


def _already_kept(sns: set[str], titles: set[tuple[str, str]], inbox: Path, post: Post) -> bool:
    """库里有、同一天同标题对得上，或归档已经完整。只有 raw、没有正文、也不是已删除的，不算。

    日常抓取和回填都走这一条。以前回填把任意 ``raw/articles/*.json`` 都当成抓完，写到一半的篇就不再取。
    """
    return (
        post.sn in sns
        or archived_post(inbox, post)
        or (_title_key(post.title), _day(published(post))) in titles
    )


def published(post: Post) -> datetime | None:
    """极致了给的时间戳坏了（特别大、不是数）当没给，不让整次抓取崩掉（审核 I5）。"""
    try:
        return datetime.fromtimestamp(post.post_time, UTC).astimezone(SHANGHAI) if post.post_time > 0 else None
    except (ArithmeticError, OSError, TypeError, ValueError):
        return None


# 归档头里这一行说明图就在公众号原文的位置，阅读页不用再按识图去挪（researchlib.render.images_placed_by_estimate）。
IMAGES_FROM_PAGE = "- 图片位置：原文页"


def archive_markdown(post: Post, body: str, author: str, *, images_from_page: bool = False) -> str:
    when = published(post)
    lines = [f"# {post.title}", "", f"- 作者：{author}"]
    if when is not None:
        lines.append(f"- 时间：{when.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    lines.append(f"- sn：`{post.sn}`")
    lines.append(f"- 原链接：{post.url}")
    lines.append(f"- 原始数据：[raw/articles/{post.sn}.json](../../raw/articles/{post.sn}.json)")
    if images_from_page:
        lines.append(IMAGES_FROM_PAGE)
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
    backfill: bool = False
    list_only: bool = False
    complete: bool = False
    listing_path: str = ""
    list_calls: int = 0
    list_cost: float = 0.0
    list_calls_total: int = 0
    list_cost_total: float = 0.0
    skip_counts: dict[str, int] = field(default_factory=dict)
    page_logs: list[str] = field(default_factory=list)
    nickname: str = ""

    def render(self) -> str:
        if self.backfill:
            return self._render_backfill()
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
            if self.failed:
                lines.append(f"失败: {len(self.failed)}")
                lines.extend(f"  ! {item}" for item in self.failed)
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

    def _render_backfill(self) -> str:
        lines = [
            f"来源: {self.source_id}（{self.source_name}）",
            "模式: 只列列表，不取正文、不写库" if self.list_only else "模式: 翻页回填",
        ]
        if self.nickname:
            lines.append(f"极致了返回的公众号: {self.nickname}")
        if self.listing_path:
            lines.append(f"列表文件: {self.listing_path}")
        lines.append(f"列出: {self.listed} 篇")
        if self.skip_counts:
            detail = "，".join(f"{name} {count}" for name, count in sorted(self.skip_counts.items()))
            lines.append(f"其中跳过: {detail}")
        lines.append(
            f"本次列表调用: {self.list_calls} 次，花费 {self.list_cost:.2f} 元"
            f"（按 {LIST_CALL_CNY:.2f} 元/次估算 {self.list_calls * LIST_CALL_CNY:.2f} 元）"
        )
        lines.append(
            f"累计列表调用: {self.list_calls_total} 次，花费 {self.list_cost_total:.2f} 元"
            f"（按 {LIST_CALL_CNY:.2f} 元/次估算 {self.list_calls_total * LIST_CALL_CNY:.2f} 元）"
        )
        if isinstance(self.remain, (int, float)) and not isinstance(self.remain, bool):
            lines.append(f"余额 {self.remain:.2f} 元")
        if self.complete:
            lines.append("列表已经翻完：再跑不会再向极致了查列表")
        elif not self.failed:
            lines.append("还没翻完：再跑同一条会从保存的 offset 接着翻，已经付过的页不会再查")
        if not self.list_only:
            lines.append(f"已在库里或归档里: {self.known}")
            if self.dry_run:
                lines.append(f"新文章: {len(self.new)}（--dry-run，没取正文、没写归档）")
                lines.extend(f"  + {item}" for item in self.new)
            else:
                lines.append(f"新写入归档: {len(self.written)}")
                lines.extend(f"  + {item}" for item in self.written)
                if self.gone:
                    lines.append(f"原文已经没了，不再取: {len(self.gone)}")
                    lines.extend(f"  - {item}" for item in self.gone)
                if self.later:
                    lines.append(f"超过这次的上限，留到下次: {self.later}")
                lines.append(
                    f"正文从原文页取不花钱；极致了正文约 {ARTICLE_CALL_CNY:.2f} 元/篇，只在原文页取不到时才调用。"
                    f"这次接口合计 {self.cost:.2f} 元（含列表）"
                )
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


def _cursor(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_is_end(value) -> bool | None:
    """极致了：1 没有下一页，0 可以继续。缺了或认不出就返回 None，调用方停下，避免同一页再扣一次。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "1":
            return True
        if text == "0":
            return False
    return None


def _optional_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _history(client, ghid: str, offset: str):
    """有 offset 才传。不传时调用方式和改分页之前一样，假的客户端可以只收一个参数。"""
    if offset:
        return client.history(ghid, offset)
    return client.history(ghid)


def history_paths(inbox: Path) -> tuple[Path, Path]:
    raw = inbox / "raw"
    return raw / LISTING_NAME, raw / STATE_NAME


def _blank_state(ghid: str, source_id: str) -> dict:
    return {
        "version": 1,
        "ghid": ghid,
        "source_id": source_id,
        "next_offset": "",
        "is_end": False,
        "blocked": "",
        "stopped_reason": "",
        "since": "",
        "list_calls": 0,
        "list_cost": 0.0,
        "requested_offsets": [],
        "remain_money": None,
        "nickname": "",
        "updated_at": "",
    }


def _load_state(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DajialaError(f"翻页进度读不出来（{path}）：{exc}。没有再向极致了查") from None
    if not isinstance(data, dict):
        raise DajialaError(f"翻页进度不是一份记录（{path}）。没有再向极致了查")
    return data


def _save_state(path: Path, state: dict) -> None:
    state["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _load_listing(path: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        sn = str(row.get("sn") or "")
        if sn and sn in seen:
            continue
        if sn:
            seen.add(sn)
        rows.append(row)
    return rows


def _append_listing(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def skip_reason(post: Post) -> str:
    """回填时不取正文的项。日常只查最新一页，不走这里。"""
    if not _HEX32.match(post.sn):
        return "没有 sn"
    if not page_allowed(post.url):
        return "不是公众号文章链接"
    raw = post.raw if isinstance(post.raw, dict) else {}
    show = _optional_int(raw.get("item_show_type"))
    if show is None and raw.get("item_show_type") not in (None, ""):
        try:
            show = int(raw.get("item_show_type"))
        except (TypeError, ValueError):
            show = None
    if show in _NON_ARTICLE_TYPES:
        return "纯视频或纯音乐"
    deleted = str(raw.get("is_deleted") or "").strip()
    status = raw.get("msg_status")
    try:
        status = int(status) if status not in (None, "") and not isinstance(status, bool) else None
    except (TypeError, ValueError):
        status = None
    if deleted == "1" or status in _DELETED_STATUS:
        return "已删除或违规"
    if status in _PENDING_STATUS:
        return "审核或发送中"
    return ""


def _listing_record(post: Post) -> dict:
    raw = post.raw if isinstance(post.raw, dict) else {}
    return {
        "sn": post.sn,
        "title": post.title,
        "url": post.url,
        "post_time": post.post_time,
        "item_show_type": raw.get("item_show_type"),
        "is_deleted": raw.get("is_deleted"),
        "msg_status": raw.get("msg_status"),
        "skip": skip_reason(post),
    }


def _post_from_record(row: dict) -> Post:
    return Post(
        url=str(row.get("url") or ""),
        title=str(row.get("title") or ""),
        post_time=_as_int(row.get("post_time")),
        sn=str(row.get("sn") or ""),
        raw=row,
    )


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _foreign_account(source_name: str, nickname: str) -> bool:
    got = re.sub(r"\s+", "", nickname or "")
    want = re.sub(r"\s+", "", source_name or "")
    if not got or not want:
        return False
    return want not in got and got not in want


def _page_older_than(posts: list[Post], since: date) -> bool:
    dates = [when.date() for post in posts if (when := published(post)) is not None]
    return bool(dates) and min(dates) < since


def _since_already_done(state: dict, since: date | None) -> bool:
    if state.get("stopped_reason") != "since" or since is None:
        return False
    saved = str(state.get("since") or "")
    try:
        return date.fromisoformat(saved) <= since
    except ValueError:
        return False


def _skip_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("skip") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _note_page(report: CrawlReport, line: str) -> None:
    report.page_logs.append(line)
    print(line, flush=True)


def _page_charge(meta: dict, before: float, after: float) -> tuple[float, str]:
    raw = meta.get("page_cost")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        charge = float(raw)
        return charge, f"这一页 {charge:.2f} 元"
    delta = after - before
    if delta:
        return float(delta), f"这一页 {delta:.2f} 元"
    return LIST_CALL_CNY, f"接口没报金额，按 {LIST_CALL_CNY:.2f} 元计"


def _apply_page_stats(report: CrawlReport, state: dict, *, calls_before: int, cost_before: float) -> None:
    report.list_calls = int(state.get("list_calls") or 0) - calls_before
    report.list_cost = round(float(state.get("list_cost") or 0) - cost_before, 4)
    report.list_calls_total = int(state.get("list_calls") or 0)
    report.list_cost_total = float(state.get("list_cost") or 0)
    report.complete = bool(state.get("is_end"))
    report.nickname = str(state.get("nickname") or "")
    remain = state.get("remain_money")
    if isinstance(remain, (int, float)) and not isinstance(remain, bool):
        report.remain = remain


def list_history(
    client,
    *,
    ghid: str,
    source,
    inbox: Path,
    report: CrawlReport,
    max_pages: int | None,
    since: date | None,
    conn,
    list_only: bool,
) -> list[dict]:
    """按 offset 翻页，成功一页就落盘。已经付过的页不重查。返回列表文件里的全部记录。"""
    listing_path, state_path = history_paths(inbox)
    report.listing_path = str(listing_path)
    loaded = _load_state(state_path)
    state = loaded or _blank_state(ghid, source.id)
    if state.get("ghid") and state.get("ghid") != ghid:
        raise DajialaError(
            f"翻页进度是另一个号的（{state.get('ghid')}），不是 {ghid}。要重头列就删掉 {state_path}"
        )
    if state.get("blocked"):
        raise DajialaError(f"{state['blocked']}。进度已停下，不会再自动扣费。确认之后删掉 {state_path} 才会重头再查")
    calls_before = int(state.get("list_calls") or 0)
    cost_before = float(state.get("list_cost") or 0)
    if state.get("is_end"):
        _note_page(report, "列表已经翻完，不再向极致了查（不重复扣费）")
        rows = _load_listing(listing_path)
        _apply_page_stats(report, state, calls_before=calls_before, cost_before=cost_before)
        report.listed = len(rows)
        report.skip_counts = _skip_counts(rows)
        return rows
    if _since_already_done(state, since):
        _note_page(report, f"列表已经翻到 {state.get('since')} 以前，不再往下查（不重复扣费）")
        rows = _load_listing(listing_path)
        _apply_page_stats(report, state, calls_before=calls_before, cost_before=cost_before)
        report.listed = len(rows)
        report.skip_counts = _skip_counts(rows)
        return rows
    if max_pages == 0:
        rows = _load_listing(listing_path)
        _apply_page_stats(report, state, calls_before=calls_before, cost_before=cost_before)
        report.listed = len(rows)
        report.skip_counts = _skip_counts(rows)
        _note_page(report, "这次 --max-pages 0，不再向极致了查列表")
        return rows

    seen = {str(row.get("sn") or "") for row in _load_listing(listing_path)}
    seen.discard("")
    requested = [str(item) for item in state.get("requested_offsets") or []]
    requested_set = set(requested)
    offset = str(state.get("next_offset") or "")
    pages_this_run = 0
    while True:
        if max_pages is not None and pages_this_run >= max_pages:
            state["stopped_reason"] = "max_pages"
            _save_state(state_path, state)
            break
        if offset in requested_set:
            state["blocked"] = "下一页的 offset 已经查过，再查会重复扣费"
            state["stopped_reason"] = "blocked"
            _save_state(state_path, state)
            raise DajialaError(f"{state['blocked']}。进度已停下，不会再自动扣费")
        before = float(getattr(client, "cost", 0.0) or 0.0)
        posts, meta = _history(client, ghid, offset)
        after = float(getattr(client, "cost", before) or before)
        pages_this_run += 1
        nickname = str(meta.get("nickname") or "")
        if nickname and _foreign_account(source.name, nickname) and not calls_before and pages_this_run == 1:
            report.failed.append(
                f"极致了返回的公众号是「{nickname}」，配置里是「{source.name}」（ghid {ghid}）。"
                "多半不是这个号，这一页没有写入列表，也没有往下翻"
            )
            report.remain = meta.get("remain_money")
            report.list_calls = 1
            report.list_calls_total = 1
            report.list_cost = (after - before) or LIST_CALL_CNY
            report.list_cost_total = report.list_cost
            report.cost = float(getattr(client, "cost", 0.0) or 0.0)
            return _load_listing(listing_path)
        if conn is not None and not list_only and not calls_before and pages_this_run == 1 and posts:
            sns, titles = known_articles(conn, source.id)
            fresh = []
            known_here = 0
            for post in posts:
                if not _HEX32.match(post.sn):
                    continue
                if post.sn in sns or (_title_key(post.title), _day(published(post))) in titles:
                    known_here += 1
                else:
                    fresh.append(post)
            if fresh and wrong_account(fresh, known_here, newest_article(conn, source.id)):
                report.failed.append(
                    f"列表里 {len(fresh)} 篇，有比库里最新一篇还早的，却一篇都对不上库里的："
                    f"多半不是这个号（ghid {ghid}），这次一篇都不取"
                )
                report.remain = meta.get("remain_money")
                report.list_calls = 1
                report.list_calls_total = 1
                report.list_cost = (after - before) or LIST_CALL_CNY
                report.list_cost_total = report.list_cost
                report.cost = float(getattr(client, "cost", 0.0) or 0.0)
                return _load_listing(listing_path)
        if not posts and not seen and not calls_before and pages_this_run == 1:
            report.failed.append(
                f"极致了列表里一篇能用的都没有（ghid {ghid}）：账号被封、接口格式改了或 ghid 写错，"
                "不当成没有新文章"
            )
            report.remain = meta.get("remain_money")
            report.list_calls = 1
            report.list_calls_total = 1
            report.list_cost = (after - before) or LIST_CALL_CNY
            report.list_cost_total = report.list_cost
            report.cost = float(getattr(client, "cost", 0.0) or 0.0)
            return []

        records = []
        for post in posts:
            if post.sn and post.sn in seen:
                continue
            if post.sn:
                seen.add(post.sn)
            records.append(_listing_record(post))
        _append_listing(listing_path, records)
        charge, charge_text = _page_charge(meta, before, after)
        state["ghid"] = ghid
        state["source_id"] = source.id
        state["list_calls"] = int(state.get("list_calls") or 0) + 1
        state["list_cost"] = round(float(state.get("list_cost") or 0) + charge, 4)
        state["requested_offsets"] = [*requested, offset]
        requested.append(offset)
        requested_set.add(offset)
        if nickname:
            state["nickname"] = nickname
        if isinstance(meta.get("remain_money"), (int, float)) and not isinstance(meta.get("remain_money"), bool):
            state["remain_money"] = meta.get("remain_money")
        estimated = round(state["list_calls"] * LIST_CALL_CNY, 2)
        total_note = ""
        if isinstance(meta.get("total_num"), int):
            total_note = f"，接口报一共 {meta['total_num']} 篇"
        raw_count = meta.get("raw_count") or 0
        dropped = max(0, int(raw_count) - len(posts)) if isinstance(raw_count, int) else 0
        dropped_note = f"，另有 {dropped} 条没有链接或标题" if dropped else ""
        _note_page(
            report,
            f"列表第 {state['list_calls']} 页：本页 {len(posts)} 篇，新记 {len(records)} 篇，"
            f"{charge_text}{total_note}{dropped_note}；累计列表调用 {state['list_calls']} 次，"
            f"合计 {state['list_cost']:.2f} 元，按 {LIST_CALL_CNY:.2f} 元/次估算 {estimated:.2f} 元",
        )
        ended = meta.get("is_end")
        if not isinstance(ended, bool):
            ended = _parse_is_end(ended)
        next_offset = str(meta.get("offset") or "")
        if ended is True:
            state["is_end"] = True
            state["next_offset"] = ""
            state["stopped_reason"] = "is_end"
            state["blocked"] = ""
            _save_state(state_path, state)
            break
        bad_cursor = ended is not False or not next_offset or next_offset in requested_set or next_offset == offset
        if bad_cursor:
            if ended is False:
                state["blocked"] = "接口说还有下一页，但没给新的 offset，再查会重复扣费"
            else:
                state["blocked"] = "接口没说明还有没有下一页（缺 is_end），先停，避免同一页再扣一次"
            state["stopped_reason"] = "blocked"
            state["is_end"] = False
            _save_state(state_path, state)
            raise DajialaError(f"{state['blocked']}。这一页已经记下，不会再自动扣费")
        if since is not None and _page_older_than(posts, since):
            state["next_offset"] = next_offset
            state["stopped_reason"] = "since"
            state["since"] = since.isoformat()
            state["is_end"] = False
            state["blocked"] = ""
            _save_state(state_path, state)
            break
        state["next_offset"] = next_offset
        state["stopped_reason"] = ""
        state["is_end"] = False
        state["blocked"] = ""
        _save_state(state_path, state)
        offset = next_offset

    rows = _load_listing(listing_path)
    _apply_page_stats(report, state, calls_before=calls_before, cost_before=cost_before)
    report.listed = len(rows)
    report.skip_counts = _skip_counts(rows)
    return rows


def _save_post(report: CrawlReport, post: Post, *, source, inbox: Path, client, fetch) -> None:
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
        return
    if not body:
        try:
            detail = client.article(post.url)
        except DajialaError as exc:
            report.failed.append(f"{_label(post)}：{unavailable}；{exc}")
            return
        body, images = dajiala_markdown(str(detail.get("content") or ""))
        body_from = "dajiala"
        if not body:
            report.failed.append(f"{_label(post)}：{unavailable}；极致了的正文也是空的")
            return
    _write(raw_path, json.dumps(_raw_record(post, body_from=body_from, detail=detail), ensure_ascii=False))
    _write(
        archive_path(inbox, post),
        archive_markdown(post, body, source.author, images_from_page=body_from == "page"),
    )
    where = "原文页" if body_from == "page" else "极致了正文，图片位置是估的"
    report.written.append(f"{_label(post)}（{where}，{len(images)} 张图）")


def _take_listed(
    report: CrawlReport,
    rows: list[dict],
    *,
    conn,
    source,
    inbox: Path,
    client,
    fetch,
    max_new: int | None,
    dry_run: bool,
    since: date | None,
) -> None:
    posts = [_post_from_record(row) for row in rows]
    posts.sort(key=lambda post: post.post_time, reverse=True)
    sns, titles = known_articles(conn, source.id)
    for post in posts:
        reason = str(post.raw.get("skip") or "") if isinstance(post.raw, dict) else ""
        if not reason:
            reason = skip_reason(post)
        if reason:
            continue
        when = published(post)
        if since is not None and when is not None and when.date() < since:
            continue
        if not _HEX32.match(post.sn):
            report.failed.append(f"{_label(post)}：列表里没有 sn，认不出是哪篇")
            continue
        if _already_kept(sns, titles, inbox, post):
            report.known += 1
            continue
        if dry_run:
            report.new.append(_label(post))
            continue
        if max_new is not None and len(report.written) + len(report.failed) >= max_new:
            report.later += 1
            continue
        sns.add(post.sn)
        _save_post(report, post, source=source, inbox=inbox, client=client, fetch=fetch)


def crawl(
    conn,
    source,
    *,
    ghid: str,
    inbox: Path,
    client: Dajiala,
    fetch=fetch_page,
    max_new: int | None = 5,
    dry_run: bool = False,
    backfill: bool = False,
    list_only: bool = False,
    max_pages: int | None = None,
    since: date | None = None,
) -> CrawlReport:
    report = CrawlReport(
        source_id=source.id,
        source_name=source.name,
        dry_run=dry_run,
        backfill=backfill or list_only,
        list_only=list_only,
    )
    if backfill or list_only:
        rows = list_history(
            client,
            ghid=ghid,
            source=source,
            inbox=inbox,
            report=report,
            max_pages=max_pages,
            since=since,
            conn=None if list_only else conn,
            list_only=list_only,
        )
        if list_only or report.failed:
            report.cost = float(getattr(client, "cost", report.cost) or 0.0)
            return report
        _take_listed(
            report,
            rows,
            conn=conn,
            source=source,
            inbox=inbox,
            client=client,
            fetch=fetch,
            max_new=max_new,
            dry_run=dry_run,
            since=since,
        )
        report.cost = float(getattr(client, "cost", 0.0) or 0.0)
        return report
    if max_new is None:
        max_new = 5
    posts, meta = client.history(ghid)
    nickname = meta.get("nickname") or ""
    if _foreign_account(source.wechat_nickname, nickname):
        raise DajialaError(
            f"极致了返回的公众号是「{nickname}」，配置里是「{source.wechat_nickname}」（ghid {ghid}），停止本次抓取"
        )
    report.remain = meta.get("remain_money")
    report.listed = len(posts)
    if not posts:
        report.failed.append(
            f"极致了列表里一篇能用的都没有（ghid {ghid}）：账号被封、接口格式改了或 ghid 写错，"
            "不当成没有新文章"
        )
        report.cost = client.cost
        return report
    sns, titles = known_articles(conn, source.id)
    fresh: list[Post] = []
    for post in posts:
        if not _HEX32.match(post.sn):
            report.failed.append(f"{_label(post)}：列表里没有 sn，认不出是哪篇")
            continue
        if _already_kept(sns, titles, inbox, post):
            report.known += 1
            continue
        fresh.append(post)
    if fresh and wrong_account(fresh, report.known, newest_article(conn, source.id)):
        report.failed.append(
            f"列表里 {len(fresh)} 篇，有比库里最新一篇还早的，却一篇都对不上库里的："
            f"多半不是这个号（ghid {ghid}），这次一篇都不取"
        )
        report.cost = client.cost
        return report
    for post in fresh:
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
        _write(
            archive_path(inbox, post),
            archive_markdown(post, body, source.author, images_from_page=body_from == "page"),
        )
        where = "原文页" if body_from == "page" else "极致了正文，图片位置是估的"
        report.written.append(f"{_label(post)}（{where}，{len(images)} 张图）")
    report.cost = client.cost
    return report


def _parse_since(text: str) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公众号定时抓取：极致了查最新文章，原文页取正文，写归档再导入")
    parser.add_argument("--source", required=True, help="config/sources.yaml 里写了 ghid 的来源")
    parser.add_argument(
        "--max-new",
        type=int,
        default=None,
        help="这次最多取几篇新文章的正文。日常默认 5；--backfill 不写就不限",
    )
    parser.add_argument("--dry-run", action="store_true", help="只查列表、说哪些是新的（列表照样收费）")
    parser.add_argument("--no-import", action="store_true", help="只写归档，不导入")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="按 offset 翻完历史列表再取正文。定时抓取不要加。每页约 0.14 元，中断可续",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="只把历史列表写进 data/inbox/<来源>/raw/post-history.jsonl，不取正文、不连库（列表照样收费）",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="这次最多向极致了新查几页。0 表示不再查，只用已经写下的列表。只和 --backfill / --list-only 一起用",
    )
    parser.add_argument(
        "--since",
        default="",
        help="翻到这个日期（上海，YYYY-MM-DD）为止，更早的不继续翻。只和 --backfill / --list-only 一起用",
    )
    args = parser.parse_args(argv)
    paging = bool(args.backfill or args.list_only)
    if args.max_pages is not None and args.max_pages < 0:
        print("拒绝：--max-pages 不能是负数", file=sys.stderr)
        return 2
    if (args.max_pages is not None or args.since) and not paging:
        print(
            "拒绝：--max-pages 和 --since 只和 --backfill 或 --list-only 一起用。"
            "日常抓取只查最新一页，避免把历史列表的费用算进定时任务",
            file=sys.stderr,
        )
        return 2
    since = _parse_since(args.since)
    if args.since and since is None:
        print(f"拒绝：--since 要写成 YYYY-MM-DD，收到 {args.since!r}", file=sys.stderr)
        return 2
    if args.max_new is None:
        max_new = None if paging else 5
    else:
        max_new = max(args.max_new, 0)

    from researchlib.config import Settings
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
    if paging:
        print(
            f"回填列表：每查一页约 {LIST_CALL_CNY:.2f} 元（--list-only 和 --dry-run 也查）。"
            "进度写在归档目录的 raw/post-history-state.json，中断再跑不会重查已记下的页。"
            "不带 --backfill / --list-only 的定时抓取仍只查最新一页。",
            flush=True,
        )

    def run(conn):
        return crawl(
            conn,
            source,
            ghid=source.ghid,
            inbox=inbox,
            client=client,
            fetch=fetch_page,
            max_new=max_new,
            dry_run=args.dry_run and not args.list_only,
            backfill=paging,
            list_only=args.list_only,
            max_pages=args.max_pages,
            since=since,
        )

    if args.list_only:
        try:
            report = run(None)
        except DajialaError as exc:
            print(f"抓取失败：{exc}（这次花费 {client.cost:.2f} 元）", file=sys.stderr)
            return 1
        print(report.render())
        return 1 if report.failed else 0

    from researchlib.db import prepare
    from researchlib.ingest.cli import run_markdown_ingest

    conn = prepare(settings)
    try:
        report = run(conn)
    except DajialaError as exc:
        print(f"抓取失败：{exc}（这次花费 {client.cost:.2f} 元）", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(report.render())
    if args.dry_run or args.no_import or not inbox.is_dir():
        if paging and args.no_import and not report.failed:
            print(
                f"没有导入。归档在 {inbox}。同意 LLM 费用之前保持这样："
                "导入之后，夜间任务才会按每晚的上限开始写摘要和判断。费用用 research-llm-estimate 看。",
                flush=True,
            )
        return 1 if report.failed else 0
    if paging:
        print(
            "开始导入。这一步不调用模型。导入之后，夜间任务和晚点的定时抓取会按各自的篇数上限写摘要、抽判断，"
            "不会一次跑完。先看费用：research-llm-estimate。",
            flush=True,
        )
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

"""给库里只有日期的 Stratechery 补上真实发布时刻。

归档只记了美国当地的日历日（frontmatter 里 ``published: 2025-09-22``），入库成上海当天零点，
``time_known`` 是 false，卡片和文章页的钟点只能写「—」。2026-11-07 线上 2409 篇全是这样。

Stratechery 官网的 WordPress 接口（``/wp-json/wp/v2/posts``）公开每篇的发布时刻 ``date_gmt``。
只取元数据（slug、当地日期、发布时刻、标题），不取正文。按 slug 对上库里那一篇，官网的当地日期
也得和库里的日历日一样才写；外部 id 里没有 slug 的，用标题加日历日对，只认唯一的一篇。对上的：
``published_at`` 换成那个时刻，``raw_meta.time_known`` 记 true，原来的日历日记进
``raw_meta.calendar_day``。美国下午、晚上发的（约 9%），按上海时间是第二天，按天的页面上挪到
第二天，和别的来源一样按上海日历排。

写库、对齐哈希走 ``retime.refresh_clocks``：观点、摘要、译文的 ``content_hash`` 跟过去，
夜间任务不会把「只改了钟点」当成正文变了去重抽、重译；态度变化按新时刻重走一遍，不调模型。
重新导入同一份归档时，``ingest.store`` 看到归档还是只给这个日历日，留着库里的时刻。

只看库里还没有钟点的篇，都有了就不连官网。夜间任务每晚跑一次（``scripts/nightly.sh``），
新导入的第二天早上就有钟点。

    docker compose exec app research-stratechery-times --dry-run
    docker compose exec app research-stratechery-times
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from researchlib.config import Settings
from researchlib.ingest.model import ParsedDoc
from researchlib.textutil import SHANGHAI

SOURCE_ID = "stratechery"
WP_POSTS = "https://stratechery.com/wp-json/wp/v2/posts"
_UA = "research-stratechery-times/1.0 (+https://investment.zldong.com)"
_PER_PAGE = 100
_RETRY_CODES = frozenset({429, 500, 502, 503, 504})
_NON_WORD = re.compile(r"[^\w]+")
_TAG = re.compile(r"<[^>]+>")
# 要查的日子隔得不到这么多天就并成一段查，隔得远的分开查：只剩几篇对不上时每晚只问一两页，不把十几年翻一遍。
_RANGE_GAP = timedelta(days=31)

Getter = Callable[[str], tuple[object, dict[str, str]]]


def _get_json(url: str, attempts: int = 4) -> tuple[object, dict[str, str]]:
    """GET 一页 JSON，返回内容和小写的响应头。限流、服务端错误、断线隔 2、4、8 秒重试。"""
    delay = 2.0
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                return json.loads(response.read().decode("utf-8")), headers
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_CODES or attempt == attempts - 1:
                raise
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("重试次数用完")


def fetch_posts(first: date, last: date, *, get: Getter = _get_json, pause: float = 1.0) -> list[dict]:
    """官网当地日期落在 first 到 last（含）的全部文章：slug、date、date_gmt、标题。每页 100 篇。"""
    params = {
        "per_page": _PER_PAGE,
        # after、before 比的是官网当地时间，前后各多留一天。
        "after": f"{(first - timedelta(days=1)).isoformat()}T00:00:00",
        "before": f"{(last + timedelta(days=2)).isoformat()}T00:00:00",
        "orderby": "date",
        "order": "asc",
        "_fields": "slug,date,date_gmt,title",
    }
    posts: list[dict] = []
    page = pages = 1
    while page <= pages:
        payload, headers = get(f"{WP_POSTS}?{urllib.parse.urlencode({**params, 'page': page})}")
        if not isinstance(payload, list):
            raise ValueError(f"官网接口第 {page} 页没回文章列表")
        posts.extend(item for item in payload if isinstance(item, dict))
        pages = int(headers.get("x-wp-totalpages") or page)
        page += 1
        if page <= pages and pause:
            time.sleep(pause)
    return posts


def slug_of(external_id: str) -> str:
    """``2025/nvidia-and-intel`` 或 ``2025/nvidia-and-intel.md`` 取 ``nvidia-and-intel``。"""
    name = (external_id or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".markdown", ".md"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def title_key(text: str) -> str:
    """标题比对用：去掉 HTML 标签和实体、全半角和标点差别（官网标题里有 ``&#8217;``、``<strike>``）。"""
    text = unicodedata.normalize("NFKC", html.unescape(_TAG.sub("", text or "")))
    return " ".join(_NON_WORD.sub(" ", text).lower().split())


def day_ranges(days: list[date]) -> list[tuple[date, date]]:
    """要查的日子并成几段：相邻两天隔得不到 _RANGE_GAP 就算一段。"""
    ranges: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if ranges and day - ranges[-1][1] <= _RANGE_GAP:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def calendar_day(published_at: datetime) -> str:
    return published_at.astimezone(SHANGHAI).date().isoformat()


def _at_midnight(published_at: datetime) -> bool:
    local = published_at.astimezone(SHANGHAI)
    return (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)


def rows_without_clock(conn) -> list[dict]:
    """库里这个来源还只有日历日的篇：上海零点、没标 time_known。没有日期的不动。"""
    rows = conn.execute(
        """
        SELECT id, external_id, title, published_at
        FROM articles
        WHERE source_id = %s AND published_at IS NOT NULL
          AND coalesce(raw_meta->>'time_known', '') <> 'true'
        ORDER BY published_at, id
        """,
        (SOURCE_ID,),
    ).fetchall()
    return [row for row in rows if _at_midnight(row["published_at"])]


def _post_title(post: dict) -> str:
    title = post.get("title")
    if isinstance(title, dict):
        title = title.get("rendered")
    return str(title or "")


def _instant(value: str) -> datetime:
    found = datetime.fromisoformat(value)
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def clock_docs(rows: list[dict], posts: list[dict]) -> tuple[list[ParsedDoc], list[int]]:
    """对上的篇给出带真实时刻的 ParsedDoc（交给 retime.refresh_clocks），对不上的给出文章 id。"""
    by_slug: dict[str, list[dict]] = {}
    by_title: dict[tuple[str, str], list[dict]] = {}
    for post in posts:
        local = str(post.get("date") or "")[:10]
        if not local or not post.get("date_gmt"):
            continue
        slug = str(post.get("slug") or "")
        if slug:
            by_slug.setdefault(slug, []).append(post)
        by_title.setdefault((title_key(_post_title(post)), local), []).append(post)
    docs: list[ParsedDoc] = []
    missing: list[int] = []
    for row in rows:
        day = calendar_day(row["published_at"])
        found = [post for post in by_slug.get(slug_of(row["external_id"]), []) if str(post["date"])[:10] == day]
        if len(found) != 1:
            found = by_title.get((title_key(row["title"] or ""), day), [])
        if len(found) != 1:
            missing.append(row["id"])
            continue
        docs.append(
            ParsedDoc(
                external_id=row["external_id"],
                title=row["title"],
                published_at=_instant(str(found[0]["date_gmt"])),
                body_md="",
                raw_meta={"date_source": "wordpress", "time_known": True, "calendar_day": day},
                image_refs=[],
            )
        )
    return docs, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 Stratechery 官网的发布时刻，给库里只有日期的篇补上钟点")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args(argv)
    from researchlib.db import prepare
    from researchlib.retime import refresh_clocks, render_stats
    from researchlib.sources import SourceRejected, load_sources, require_source

    settings = Settings.from_env()
    try:
        source = require_source(load_sources(settings.sources_path), SOURCE_ID, kind="markdown")
    except SourceRejected as exc:
        print(f"跳过：{exc}")
        return 0
    conn = prepare(settings)
    try:
        rows = rows_without_clock(conn)
        if not rows:
            print("Stratechery 每篇都有钟点了，不连官网。")
            return 0
        days = [date.fromisoformat(calendar_day(row["published_at"])) for row in rows]
        posts = [post for first, last in day_ranges(days) for post in fetch_posts(first, last)]
        docs, missing = clock_docs(rows, posts)
        stats = refresh_clocks(
            conn, source, docs, settings, dry_run=args.dry_run, same_row=True, forget_index=False
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(render_stats(stats))
    print(f"官网接口: {len(posts)} 篇；库里只有日期的 {len(rows)} 篇，对上 {len(docs)} 篇，没对上 {len(missing)} 篇")
    if missing:
        shown = ", ".join(str(item) for item in missing[:30])
        print(f"  没对上的文章 id: {shown}{' …' if len(missing) > 30 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

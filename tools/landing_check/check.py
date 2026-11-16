"""检索结果点进去落不落得到那一段：逐个打开落点，看高亮标没标上、页面多大、服务器用了多久。

只读线上公开页面，不调模型。用法：

    python tools/landing_check/check.py --base https://investment.zldong.com --out data/quality-eval/landing.json
    python tools/landing_check/check.py --links links.txt   # 一行一个 /articles/{id}?chunk=… 落点

不给 --links 时，取 tools/quality_eval/queries.json 里 24 个问题，各取检索页前 10 条结果的链接。
图片文字块（块号 20000 起）按设计跳到那张图、不标高亮，单独计数，不算失败。
超长文的页面一次几 MB、服务器几秒，别并发拉。
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUGE = 1_000_000  # 页面字符数过这条线算超长文


def get(base: str, path: str) -> tuple[str, float, float | None]:
    url = base.rstrip("/") + path
    # Cloudflare 拦 Python 默认的 User-Agent，换一个说明用途的名字。
    request = urllib.request.Request(url, headers={"User-Agent": "research-quality-eval/1"})
    for attempt in range(4):
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                body = response.read().decode("utf-8", "replace")
                timing = response.headers.get("Server-Timing") or ""
            match = re.search(r"dur=([\d.]+)", timing)
            return body, time.time() - started, float(match.group(1)) / 1000 if match else None
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def search_links(base: str, per_query: int) -> list[str]:
    queries = json.loads((HERE.parent / "quality_eval" / "queries.json").read_text())
    links: list[str] = []
    for query in queries["tune"] + queries["hold"]:
        page, _elapsed, _server = get(base, "/search?" + urllib.parse.urlencode({"q": query}))
        found = re.findall(r'<h3 class="item-title".*?<a href="([^"]+)">', page, re.S)
        for link in found[:per_query]:
            link = html_lib.unescape(link).split("#")[0]
            if link not in links:
                links.append(link)
        time.sleep(0.5)
    return links


def check(base: str, link: str) -> dict:
    body, elapsed, server = get(base, link)
    chunk = re.search(r"[?&]chunk=(\d+)", link)
    ord_ = int(chunk.group(1)) if chunk else -1
    return {
        "link": link,
        "ocr": ord_ >= 20000,
        "marked": len(re.findall(r'class="[^"]*\bis-quoted\b', body)),
        "aside": 'class="quoted"' in body,
        "size": len(body),
        "seconds": round(elapsed, 2),
        "server": server,
    }


def summarize(rows: list[dict]) -> str:
    text = [row for row in rows if not row["ocr"] and "error" not in row]
    huge = [row for row in text if row["size"] >= HUGE]

    def rate(group: list[dict]) -> str:
        hit = sum(1 for row in group if row["marked"])
        return f"{hit}/{len(group)}" + (f"（{hit / len(group):.0%}）" if group else "")

    servers = sorted(row["server"] for row in text if row["server"] is not None)
    middle = servers[len(servers) // 2] if servers else None
    lines = [
        f"落点 {len(rows)} 个，图片文字块 {sum(row['ocr'] for row in rows)} 个不算，"
        f"出错 {sum('error' in row for row in rows)} 个",
        f"标中 {rate(text)}；超长文（页面 {HUGE:,} 字符以上）{rate(huge)}",
        f"服务器用时中位 {middle:.2f} 秒" if middle is not None else "服务器用时：页面没给 Server-Timing",
        f"超长文页面最大 {max((row['size'] for row in huge), default=0):,} 字符",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="https://investment.zldong.com")
    parser.add_argument("--links", type=Path, help="一行一个落点；不给就从检索页取")
    parser.add_argument("--per-query", type=int, default=10)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    links = ([line.strip() for line in args.links.read_text().splitlines() if line.strip()]
             if args.links else search_links(args.base, args.per_query))
    rows = []
    for link in links:
        try:
            rows.append(check(args.base, link))
        except Exception as error:  # 一个落点出错不中断整批
            rows.append({"link": link, "ocr": False, "error": str(error)})
        time.sleep(0.8)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(summarize(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())

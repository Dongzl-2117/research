"""正文里的外链图片下载到本地。

AIHOT 导入的文章不带图，Markdown 里的 http 图片导入时也不下载：页面上原来只显示「外部图片已省略」。
原文删了以后图床地址也可能失效，图表和截图就没了。这里把它们下载下来，按内容哈希存进
DATA_DIR/images/remote/，在 article_images 里记一行（remote = true），文章页和备份都和归档里的图一样对待。

- 只下 http(s)，只连公网地址：每次建连接时核对对方的 IP，内网、本机、Tailscale 这类地址一律断开，
  跳转、域名解析结果中途变了也一样；不走代理，最多跳转 5 次。只收图片，单张不超过 15MB。
- WordPress 图片 CDN（i0.wp.com/原站/路径，Stratechery 的图都是这样）先下原站那一张，下不来再试 CDN：
  Mac mini 上的代理把 i0.wp.com 解析成 198.18.x.x，上面那条检查不放行；CDN 对一部分图还会回 404。
- 不带 Referer（公众号图床对外链 Referer 返回占位图）。
- 每个地址的结果记在 image_fetches：成功的别的文章引用同一张图直接复用；失败的按次数隔 1、3、7、30 天再试。
- 每次跑完把新地址下不来的张数、原因类别和最多的域名写进 DATA_DIR/status/fetch-images.json，
  新地址大半下不来时 research-status 会列出来，值班不用查库（见 fetch_status）。

    research-fetch-images --limit 300
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from researchlib.chunks import replace_ocr_chunks
from researchlib.render import img_tag_url, normalize_image_url

MAX_BYTES = 15 * 1024 * 1024
TIMEOUT_S = 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
# 第 n 次失败后隔多少天再试。
RETRY_DAYS = (1, 3, 7, 30)

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
# 先在库里粗筛有外链图的文章（Markdown 图片或 <img>），再逐篇精确抽地址。
PREFILTER = r"(!\[[^]]*\]\((https?:)?//)|(<img[^>]+src=[\"']?(https?:)?//)"

_SNIFF = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


class FetchError(Exception):
    pass


def external_urls(body: str) -> list[str]:
    """正文里的外链图片地址，按出现顺序去重。"""
    found: list[str] = []
    for match in _MD_IMAGE.finditer(body or ""):
        url = normalize_image_url(match.group(1))
        if url and url not in found:
            found.append(url)
    for match in _IMG_TAG.finditer(body or ""):
        url = normalize_image_url(img_tag_url(match.group(0)))
        if url and url not in found:
            found.append(url)
    return found


def image_extension(data: bytes, content_type: str = "") -> str:
    for magic, ext in _SNIFF:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    subtype = (content_type or "").split(";", 1)[0].strip().lower()
    if subtype.startswith("image/"):
        guess = subtype.split("/", 1)[1]
        return {"jpeg": ".jpg", "svg+xml": ".svg"}.get(guess, "." + re.sub(r"[^a-z0-9]", "", guess) or ".img")
    return ""


_NOT_PUBLIC = "地址解析到内网或本机，不下载"
# 解析不了（域名没了、这台机器的 DNS 坏了）和解析到内网（代理的 fake-ip）分开记：处理办法不一样。
_NO_DNS = "域名解析不了"


def blocked_address(value: str) -> bool:
    """不是公网地址：内网、本机、链路本地、运营商级 NAT（Tailscale 的 100.64.0.0/10）、组播、保留段。"""
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not address.is_global or address.is_multicast


def _public_host(host: str) -> bool:
    """解析出来全是公网地址。解析不了抛 FetchError（「域名解析不了」）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchError(f"{_NO_DNS}：{exc}") from exc
    return bool(infos) and not any(blocked_address(str(info[4][0])) for info in infos)


def _checked_create_connection(address, *args, **kwargs):
    """建好 TCP 连接后核对对方的 IP，不是公网就断开：跳转和中途换了解析结果都拦得住。"""
    sock = socket.create_connection(address, *args, **kwargs)
    try:
        peer = sock.getpeername()[0]
    except OSError:
        sock.close()
        raise
    if blocked_address(str(peer)):
        sock.close()
        raise FetchError(_NOT_PUBLIC)
    return sock


class _CheckedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _checked_create_connection


class _CheckedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _checked_create_connection


class _CheckedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_CheckedHTTPConnection, req)


class _CheckedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_CheckedHTTPSConnection, req, context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in ("http", "https"):
            raise FetchError("跳转到了 http(s) 以外的地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    """只有 http(s)、不走代理（没有 ProxyHandler）、每次建连接都核对对方地址。"""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.UnknownHandler(),
        _CheckedHTTPHandler(),
        _CheckedHTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _Redirects(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


# WordPress 的图片 CDN（Photon）：i0.wp.com/<原站>/<路径>，原图就在 https://<原站>/<路径>。
_PHOTON_HOST = re.compile(r"^i[0-3]\.wp\.com$", re.I)


def photon_origin(url: str) -> str:
    """i0.wp.com/stratechery.com/wp-content/… 的原站地址（去掉缩放参数）。不是这种地址返回空串。"""
    parts = urlsplit(url)
    if not _PHOTON_HOST.match(parts.hostname or ""):
        return ""
    host, _, rest = parts.path.lstrip("/").partition("/")
    if "." not in host or not rest:
        return ""
    return f"https://{host}/{rest}"


def download(url: str) -> tuple[bytes, str]:
    """下载一张图，返回 (内容, Content-Type)。不是图片、太大、地址不对都抛 FetchError。

    WordPress 图片 CDN 上的图先下原站那一张，下不来再试 CDN，两边的原因都记下。
    """
    origin = photon_origin(url)
    if not origin:
        return _download(url)
    try:
        return _download(origin)
    except FetchError as first:
        try:
            return _download(url)
        except FetchError as second:
            raise FetchError(f"原站：{first}；{urlsplit(url).hostname}：{second}") from second


def _download(url: str) -> tuple[bytes, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("不是 http(s) 地址")
    if not _public_host(parts.hostname):
        raise FetchError(_NOT_PUBLIC)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5"})
    try:
        with _opener().open(request, timeout=TIMEOUT_S) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"连不上：{exc}") from exc
    if len(data) > MAX_BYTES:
        raise FetchError("超过 15MB")
    if not image_extension(data, content_type):
        raise FetchError(f"不是图片（{content_type or '没有 Content-Type'}）")
    return data, content_type


def store_bytes(data_dir: Path, data: bytes, ext: str) -> tuple[str, str]:
    """按内容哈希存，返回 (哈希, 相对 images/ 的路径)。同一张图只存一份。"""
    digest = hashlib.sha256(data).hexdigest()
    relative = f"remote/{digest[:2]}/{digest}{ext}"
    target = Path(data_dir) / "images" / relative
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return digest, relative


def _link(conn, article_id: int, source_id: str, url: str, relative: str, digest: str) -> bool:
    row = conn.execute(
        """
        INSERT INTO article_images (article_id, source_id, original_ref, storage_path, content_hash, remote)
        VALUES (%s, %s, %s, %s, %s, true)
        ON CONFLICT (article_id, original_ref) DO NOTHING
        RETURNING id
        """,
        (article_id, source_id, url, relative, digest),
    ).fetchone()
    return row is not None


def _cooling(state: dict | None) -> bool:
    """失败过、还没到下次重试时间的地址。"""
    if state is None or state["ok"]:
        return False
    days = RETRY_DAYS[min(len(RETRY_DAYS), max(1, int(state["attempts"]))) - 1]
    return bool(state["age_days"] is not None and state["age_days"] < days)


def _states(conn, urls: list[str]) -> dict[str, dict]:
    if not urls:
        return {}
    rows = conn.execute(
        """
        SELECT url, ok, content_hash, storage_path, attempts,
               extract(epoch FROM now() - fetched_at) / 86400 AS age_days
        FROM image_fetches WHERE url = ANY(%s)
        """,
        (urls,),
    ).fetchall()
    return {row["url"]: dict(row) for row in rows}


def _record(conn, url: str, *, ok: bool, digest: str = "", relative: str = "", error: str = "") -> None:
    conn.execute(
        """
        INSERT INTO image_fetches (url, ok, content_hash, storage_path, error, attempts, fetched_at)
        VALUES (%s, %s, %s, %s, %s, 1, now())
        ON CONFLICT (url) DO UPDATE SET
          ok = EXCLUDED.ok,
          content_hash = CASE WHEN EXCLUDED.ok THEN EXCLUDED.content_hash ELSE image_fetches.content_hash END,
          storage_path = CASE WHEN EXCLUDED.ok THEN EXCLUDED.storage_path ELSE image_fetches.storage_path END,
          error = EXCLUDED.error,
          attempts = CASE WHEN EXCLUDED.ok THEN 1 ELSE image_fetches.attempts + 1 END,
          fetched_at = now()
        """,
        (url, ok, digest, relative, error[:300]),
    )


def candidate_articles(conn, after_id: int | None, batch: int = 200):
    """有外链图片的文章（重复的不算），按 id 从新到旧分批取。"""
    return conn.execute(
        """
        SELECT a.id, a.source_id, a.body_md,
               ARRAY(SELECT i.original_ref FROM article_images i WHERE i.article_id = a.id AND i.remote) AS linked
        FROM articles a
        WHERE a.duplicate_of IS NULL AND a.body_md ~* %s AND (%s::bigint IS NULL OR a.id < %s::bigint)
        ORDER BY a.id DESC
        LIMIT %s
        """,
        (PREFILTER, after_id, after_id, batch),
    ).fetchall()


def fetch_pending(
    conn,
    data_dir: Path,
    limit: int = 300,
    fetch: Callable[[str], tuple[bytes, str]] = download,
) -> dict:
    """最多处理 limit 个还没挂上的地址。返回计数 {"downloaded", "reused", "failed", "skipped", "articles"}。

    另记第一次下的地址：new 个，其中下不来的按原因类别（kinds）和域名（hosts）计数，给 fetch_status 判断。
    重试的地址以前就下不来，再失败是意料之中，只计 retried / retry_failed。
    """
    report = {
        "downloaded": 0, "reused": 0, "failed": 0, "skipped": 0, "articles": 0,
        "new": 0, "retried": 0, "retry_failed": 0, "kinds": Counter(), "hosts": Counter(),
    }
    budget = limit
    after = None
    while budget > 0:
        rows = candidate_articles(conn, after)
        if not rows:
            break
        for row in rows:
            after = row["id"]
            linked = set(row["linked"] or [])
            todo = [url for url in external_urls(row["body_md"]) if url not in linked]
            if not todo:
                continue
            states = _states(conn, todo)
            touched = False
            for url in todo:
                if budget <= 0:
                    break
                state = states.get(url)
                if state is not None and state["ok"] and state["storage_path"]:
                    if (Path(data_dir) / "images" / state["storage_path"]).is_file():
                        touched |= _link(conn, row["id"], row["source_id"], url, state["storage_path"],
                                         state["content_hash"])
                        report["reused"] += 1
                        continue
                if _cooling(state):
                    report["skipped"] += 1
                    continue
                budget -= 1
                first = state is None
                report["new" if first else "retried"] += 1
                try:
                    data, content_type = fetch(url)
                    ext = image_extension(data, content_type)
                    if not ext:
                        raise FetchError("不是图片")
                    digest, relative = store_bytes(data_dir, data, ext)
                except FetchError as exc:
                    _record(conn, url, ok=False, error=str(exc))
                    report["failed"] += 1
                    if first:
                        report["kinds"][failure_kind(str(exc))] += 1
                        report["hosts"][urlsplit(url).hostname or "?"] += 1
                    else:
                        report["retry_failed"] += 1
                    continue
                _record(conn, url, ok=True, digest=digest, relative=relative)
                touched |= _link(conn, row["id"], row["source_id"], url, relative, digest)
                report["downloaded"] += 1
            if touched:
                # 新挂上的图如果别处已经识别过字，图片文字块马上就有。
                replace_ocr_chunks(conn, row["id"])
                report["articles"] += 1
            conn.commit()
            if budget <= 0:
                break
    return report


def failure_kind(error: str) -> str:
    """image_fetches.error → 原因类别：HTTP 404、连不上、解析到内网、域名解析不了、不是图片……

    WordPress 的图两边都试过（「原站：…；i0.wp.com：…」）：哪边回了 HTTP 状态码按哪边，服务器明确答复了；
    两边都没有按原站。
    """
    text = (error or "").strip()
    if text.startswith("原站："):
        origin, _, cdn = text[len("原站：") :].partition("；")
        kinds = [failure_kind(origin), failure_kind(cdn.partition("：")[2])]
        return next((kind for kind in kinds if kind.startswith("HTTP ")), kinds[0])
    status = re.match(r"HTTP (\d{3})", text)
    if status:
        return f"HTTP {status.group(1)}"
    for prefix, kind in (
        (_NOT_PUBLIC, "解析到内网"),
        (_NO_DNS, _NO_DNS),
        ("连不上", "连不上"),
        ("不是图片", "不是图片"),
        ("超过", "超过 15MB"),
        ("不是 http(s)", "地址不对"),
        ("跳转到了", "地址不对"),
    ):
        if text.startswith(prefix):
            return kind
    return "其他"


# 第一次下的地址够这么多个，才按比例判断这次是不是出了问题；少于这个数、又有失败的，不改上一次的结论。
STATUS_MIN_NEW = 5
# 第一次下的地址里，下不来的超过一半，或者够这么多个，就在 research-status 里列出来。
STATUS_MANY_FAILED = 20
STATUS_TASK = "fetch-images"


def fetch_status(report: dict) -> tuple[bool, str] | None:
    """这次下载算不算出了问题：(没问题, 一句话)。没有可判断的返回 None，状态文件不动。

    只看第一次下的地址：新文章的图大半下不来，多半是这台机器的网络、代理（fake-ip）、对方拦外链
    或者地址规则变了，要有人看。重试的地址本来就下不来过，再失败不算。原因类别和最多的几个域名
    写进这句话，值班看这句就知道是哪一类、哪个图床。原图已经删了（HTTP 404）也照样报：一下子很多
    也可能是图床换了地址（#256 的 CDN 404）。
    """
    new = int(report.get("new") or 0)
    kinds = Counter(report.get("kinds") or {})
    failed = sum(kinds.values())
    if new == 0 or (failed and new < STATUS_MIN_NEW):
        return None
    message = f"新地址 {new} 个，下下来 {new - failed} 个"
    if failed:
        message += "，下不来 " + "、".join(f"{kind} {count}" for kind, count in kinds.most_common())
        hosts = Counter(report.get("hosts") or {}).most_common(3)
        message += "（最多的是 " + "、".join(f"{host} {count}" for host, count in hosts) + "）"
    if report.get("retried"):
        message += f"；重试以前下不来的 {report['retried']} 个，又失败 {report.get('retry_failed', 0)} 个"
    problem = failed * 2 > new or failed >= STATUS_MANY_FAILED
    return not problem, message


def record_fetch_status(data_dir: Path, report: dict) -> str:
    """把 fetch_status 的结论写进 DATA_DIR/status/fetch-images.json，返回那句话（没有结论返回空串）。"""
    from researchlib.health import write_status

    verdict = fetch_status(report)
    if verdict is None:
        return ""
    ok, message = verdict
    write_status(
        data_dir,
        STATUS_TASK,
        ok,
        message,
        new=int(report.get("new") or 0),
        kinds=dict(report.get("kinds") or {}),
        hosts=dict(Counter(report.get("hosts") or {}).most_common(10)),
    )
    return message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把正文里的外链图片下载到本地（DATA_DIR/images/remote）")
    parser.add_argument("--limit", type=int, default=300, help="这次最多下载多少个地址")
    args = parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        report = fetch_pending(conn, settings.data_dir, args.limit)
    finally:
        conn.close()
    print(
        f"下载 {report['downloaded']} 张，复用 {report['reused']} 张，失败 {report['failed']} 张，"
        f"等下次重试 {report['skipped']} 张，涉及 {report['articles']} 篇"
    )
    summary = record_fetch_status(settings.data_dir, report)
    if summary:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""正文里的外链图片下载到本地。

AIHOT 导入的文章不带图，Markdown 里的 http 图片导入时也不下载：页面上原来只显示「外部图片已省略」。
原文删了以后图床地址也可能失效，图表和截图就没了。这里把它们下载下来，按内容哈希存进
DATA_DIR/images/remote/，在 article_images 里记一行（remote = true），文章页和备份都和归档里的图一样对待。

- 只下 http(s)，只连公网地址：每次建连接时核对对方的 IP，内网、本机、Tailscale 这类地址一律断开，
  跳转、域名解析结果中途变了也一样；不走代理，最多跳转 5 次。只收图片，单张不超过 15MB。
- 代理的 fake-ip 只给几个固定的图床放行（PROXY_IMAGE_HOSTS）。Mac mini 上的代理开着 fake-ip，容器里
  i0.wp.com、stratechery.com 都解析成 198.18.x.x，连上去由代理按域名转到真正的服务器。代理背后连的是哪里
  这边看不到，正文里随便写的域名真正的地址可能在内网，所以别的域名解析成假地址照旧不下。
  11-08 起微信图床 mmbiz.qpic.cn 也解析成假地址，新抓的公众号文章图全被拦，也放行了。
- WordPress 图片 CDN（i0.wp.com/原站/路径，Stratechery 的图都是这样）先下原站那一张，下不来再试 CDN：
  CDN 对一部分图会回 404。
- 不带 Referer（公众号图床对外链 Referer 返回占位图）。
- 每个地址的结果记在 image_fetches：成功的别的文章引用同一张图直接复用；失败的按次数隔 1、3、7、30 天再试。
- 每次跑完把新地址下不来的张数、原因类别和最多的域名写进 DATA_DIR/status/fetch-images.json，
  新地址大半下不来、或者某个图床的新地址一个都没下下来时 research-status 会列出来，值班不用查库
  （见 fetch_status）。夜间补齐带 --exit-code，这时退出码 3，那一步记成失败。

    research-fetch-images --limit 300
    research-fetch-images --limit 300 --exit-code
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
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


# 下下来的图宽乘高的上限（C9）：1.7 亿像素的纯色 PNG 不到 10MB，解码成 RGB 要 500MB，识图 8 路同时认能把容器撑爆。
MAX_PIXELS = 40_000_000
_PIL_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "BMP": ".bmp", "WEBP": ".webp", "TIFF": ".tif",
                "ICO": ".ico", "AVIF": ".avif", "HEIF": ".heic"}
_SVG_HEAD = re.compile(rb"^\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*(?:<!DOCTYPE svg[^>]*>\s*)?<svg[\s>]", re.S | re.I)


def _pixels(data: bytes) -> tuple[str, int] | None:
    """(格式, 宽乘高)；Pillow 认不出、宽高读不出来回 None。只读文件头，不解码。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            kind = image.format or ""
    except Exception:  # noqa: BLE001 — 坏图、截断的图、认不出的格式都当不是图
        return None
    if width <= 0 or height <= 0:
        return None
    return kind, width * height


def image_extension(data: bytes, content_type: str = "") -> str:
    """按文件内容认图，返回扩展名；不是图、读不出宽高、大过 MAX_PIXELS 的回 ""。

    不按 Content-Type 推：图床回的错误页、占位页常打着 image/*，以前被存成 .html、记成下好了，永不重试。
    开头碰巧是「BM」的文字也不算 BMP：Pillow 要读得出宽高。SVG 认文件开头的 <svg（/media 按沙箱回）。
    """
    del content_type  # 只看内容；参数留着给调用方
    if _SVG_HEAD.match(data[:4096]):
        return ".svg"
    found = _pixels(data)
    if found is None:
        return ""
    kind, pixels = found
    if pixels > MAX_PIXELS:
        return ""
    for magic, ext in _SNIFF:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return _PIL_FORMATS.get(kind, "")


# 解析不了（域名没了、这台机器的 DNS 坏了）、解析到代理的假地址（fake-ip）、解析到内网分开记：处理办法不一样。
# 后两种在错误后面带上「（域名 → 地址）」，查库就知道是哪个域名、解析成了什么。
_NOT_PUBLIC = "地址解析到内网或本机，不下载"
_FAKE_IP = "地址解析到代理的假地址，不下载"
_NO_DNS = "域名解析不了"

# Clash、Surge、sing-box 这类代理开 fake-ip 时用的段：RFC 2544 留给测试的 198.18.0.0/15，公网上不会出现。
PROXY_FAKE_IPS = ipaddress.ip_network("198.18.0.0/15")
# 解析成代理假地址也照样下的图床：都是固定的公网服务器，代理按域名连过去不会进内网。
# 只写正文里真有的图床（Stratechery 原站、WordPress 图片 CDN、微信公众号图床），不按后缀匹配。
PROXY_IMAGE_HOSTS = frozenset(
    {
        "stratechery.com", "www.stratechery.com", "i0.wp.com", "i1.wp.com", "i2.wp.com", "i3.wp.com",
        "mmbiz.qpic.cn",
    }
)


# 内嵌 IPv4 的 IPv6 段：NAT64（64:ff9b::/96）、旧的 IPv4 兼容地址（::/96）。网关会把它们转到内嵌的 IPv4 上，
# 按内嵌的那个判。
_EMBEDDED_V4 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::/96"))


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if any(address in network for network in _EMBEDDED_V4) and int(address) > 1:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def blocked_address(value: str) -> bool:
    """不是公网地址：内网、本机、链路本地、运营商级 NAT（Tailscale 的 100.64.0.0/10）、组播、保留段、代理假地址。"""
    address = _ip(value)
    return address is None or not address.is_global or address.is_multicast


def proxy_fake_ip(value: str) -> bool:
    """代理 fake-ip 段里的地址（198.18.0.0/15）。"""
    address = _ip(value)
    return address is not None and address in PROXY_FAKE_IPS


def check_address(host: str, address: str) -> None:
    """连 host 连到了 address：能连就返回，不能连抛 FetchError。

    公网地址都能连；代理的假地址只有 PROXY_IMAGE_HOSTS 能连，真正的内网地址谁都不能连。
    """
    if not blocked_address(address):
        return
    if proxy_fake_ip(address):
        if host.lower().rstrip(".") in PROXY_IMAGE_HOSTS:
            return
        raise FetchError(f"{_FAKE_IP}（{host} → {address}）")
    raise FetchError(f"{_NOT_PUBLIC}（{host} → {address}）")


def _check_host(host: str) -> None:
    """解析出来的地址每个都能连（check_address）。解析不了抛「域名解析不了」。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FetchError(f"{_NO_DNS}：{exc}") from exc
    if not infos:
        raise FetchError(f"{_NO_DNS}：没有地址")
    for info in infos:
        check_address(host, str(info[4][0]))


def _checked_create_connection(address, *args, **kwargs):
    """建好 TCP 连接后核对对方的 IP，不能连就断开：跳转和中途换了解析结果都拦得住。"""
    sock = socket.create_connection(address, *args, **kwargs)
    try:
        check_address(str(address[0]), str(sock.getpeername()[0]))
    except (OSError, FetchError):
        sock.close()
        raise
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
    _check_host(parts.hostname)
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
        found = _pixels(data)
        if found is not None and found[1] > MAX_PIXELS:
            raise FetchError(f"图太大（{found[1] // 1_000_000} 百万像素，上限 {MAX_PIXELS // 1_000_000}）")
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
    """image_fetches.error → 原因类别：HTTP 404、连不上、解析到代理假地址、解析到内网、域名解析不了、不是图片……

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
        (_FAKE_IP, "解析到代理假地址"),
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
# 带 --exit-code 时，新地址大半下不来（fetch_status 判的有问题）的退出码。0 是跑完了，1 是出错退出。
EXIT_MOSTLY_FAILED = 3

# 一个图床最近这几天下过的地址一张都没下下来（dead_hosts）：多半是这台机器连不上它（代理的 fake-ip、网络、
# 对方拦外链），这个图床的新图全会显示成「外部图片已省略」。按一次跑的比例看不出来：11-08 起微信图床
# 三天一张没下下来，每次只有几个，夹在几百张下下来的 Stratechery 图里，没人发现。
DEAD_HOST_DAYS = 3
# 这么多个地址都下不来才算整个图床下不来，一两张坏图不算。
DEAD_HOST_MIN = 3
# 原图删了（404、410）、域名没了：图床那边的事，这边修不了，不算图床下不来。
GONE_KINDS = frozenset({"HTTP 404", "HTTP 410", _NO_DNS})


def dead_hosts(conn, days: int = DEAD_HOST_DAYS) -> dict[str, Counter]:
    """最近 days 天下过、一张都没下下来的图床：{域名: 原因类别计数}。

    看 image_fetches 里每个地址最近一次的结果，第一次下的和重试的都算：一个图床整个下不来时，它的新图
    抓下来那一次就失败、之后都在重试，只看哪一次跑的新地址，下一次就看不到了。这几天有一张下下来了就不算；
    原因大半是原图没了、域名没了的也不算（Stratechery 的图补完以后只剩 404 的在重试，偶尔连不上几次不报）。
    """
    rows = conn.execute(
        """
        SELECT host, array_agg(error) AS errors
        FROM (
            SELECT lower(substring(url from '^[A-Za-z][A-Za-z0-9+.-]*://([^/:?#]+)')) AS host, ok, error
            FROM image_fetches
            WHERE fetched_at > now() - make_interval(days => %s)
        ) recent
        WHERE host IS NOT NULL
        GROUP BY host
        HAVING bool_and(NOT ok) AND count(*) >= %s
        """,
        (days, DEAD_HOST_MIN),
    ).fetchall()
    found: dict[str, Counter] = {}
    for row in rows:
        kinds = Counter(failure_kind(error) for error in row["errors"])
        ours = sum(count for kind, count in kinds.items() if kind not in GONE_KINDS)
        if ours >= DEAD_HOST_MIN and ours * 2 > sum(kinds.values()):
            found[row["host"]] = kinds
    return found


def _dead_hosts_message(dead: dict) -> str:
    parts = []
    for host, kinds in sorted(dead.items(), key=lambda item: (-sum(item[1].values()), item[0])):
        kinds = Counter(kinds)
        detail = "、".join(f"{kind} {count}" for kind, count in kinds.most_common())
        parts.append(f"{host} {sum(kinds.values())} 个（{detail}）")
    return f"最近 {DEAD_HOST_DAYS} 天一张都没下下来的图床：" + "、".join(parts)


def fetch_status(report: dict, dead: dict | None = None) -> tuple[bool, str] | None:
    """这次下载算不算出了问题：(没问题, 一句话)。没有可判断的返回 None，状态文件不动。

    只看第一次下的地址：新文章的图大半下不来，多半是这台机器的网络、代理（fake-ip）、对方拦外链
    或者地址规则变了，要有人看。重试的地址本来就下不来过，再失败不算。原因类别和最多的几个域名
    写进这句话，值班看这句就知道是哪一类、哪个图床。原图已经删了（HTTP 404）也照样报：一下子很多
    也可能是图床换了地址（#256 的 CDN 404）。

    dead 是 dead_hosts 查出来的整个下不来的图床：有就算出了问题，哪怕这次新地址太少、或者都是重试。
    """
    new = int(report.get("new") or 0)
    kinds = Counter(report.get("kinds") or {})
    failed = sum(kinds.values())
    if new == 0 or (failed and new < STATUS_MIN_NEW):
        if dead:
            return False, _dead_hosts_message(dead)
        return None
    message = f"新地址 {new} 个，下下来 {new - failed} 个"
    if failed:
        message += "，下不来 " + "、".join(f"{kind} {count}" for kind, count in kinds.most_common())
        hosts = Counter(report.get("hosts") or {}).most_common(3)
        message += "（最多的是 " + "、".join(f"{host} {count}" for host, count in hosts) + "）"
    if report.get("retried"):
        message += f"；重试以前下不来的 {report['retried']} 个，又失败 {report.get('retry_failed', 0)} 个"
    if dead:
        message += "；" + _dead_hosts_message(dead)
    problem = failed * 2 > new or failed >= STATUS_MANY_FAILED or bool(dead)
    return not problem, message


def record_fetch_status(data_dir: Path, report: dict, dead: dict | None = None) -> tuple[bool, str] | None:
    """把 fetch_status 的结论写进 DATA_DIR/status/fetch-images.json，返回这个结论（没有结论返回 None，文件不动）。"""
    from researchlib.health import write_status

    verdict = fetch_status(report, dead)
    if verdict is None:
        return None
    ok, message = verdict
    write_status(
        data_dir,
        STATUS_TASK,
        ok,
        message,
        new=int(report.get("new") or 0),
        kinds=dict(report.get("kinds") or {}),
        hosts=dict(Counter(report.get("hosts") or {}).most_common(10)),
        dead_hosts={host: dict(kinds) for host, kinds in (dead or {}).items()},
    )
    return verdict


def finish(data_dir: Path, report: dict, *, exit_code: bool = False, dead: dict | None = None) -> int:
    """记下结论、打出那句话，返回退出码。

    带 exit_code 且新地址大半下不来、或者有整个下不来的图床（dead）时退出码 EXIT_MOSTLY_FAILED，
    那句话打在错误输出最后一行：夜间补齐（scripts/nightly.sh）把这一步记成失败，原因就是这句。
    不带时照旧退出码 0。
    """
    verdict = record_fetch_status(data_dir, report, dead)
    if verdict is None:
        return 0
    ok, summary = verdict
    if ok or not exit_code:
        print(summary)
        return 0
    print(summary, file=sys.stderr)
    return EXIT_MOSTLY_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把正文里的外链图片下载到本地（DATA_DIR/images/remote）")
    parser.add_argument("--limit", type=int, default=300, help="这次最多下载多少个地址")
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help=(
            f"新地址大半下不来、或者有整个下不来的图床（research-status 会报的那种）时退出码 {EXIT_MOSTLY_FAILED}，"
            "原因打在错误输出最后一行"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """退出码：0 跑完了（个别下不来也算）；带 --exit-code 时新地址大半下不来、或者有整个下不来的图床是 3。"""
    args = build_parser().parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        report = fetch_pending(conn, settings.data_dir, args.limit)
        dead = dead_hosts(conn)
    finally:
        conn.close()
    print(
        f"下载 {report['downloaded']} 张，复用 {report['reused']} 张，失败 {report['failed']} 张，"
        f"等下次重试 {report['skipped']} 张，涉及 {report['articles']} 篇"
    )
    return finish(settings.data_dir, report, exit_code=args.exit_code, dead=dead)


if __name__ == "__main__":
    sys.exit(main())

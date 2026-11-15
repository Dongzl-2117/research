"""样式和脚本带内容版本号：带当前版本号来取的一直缓存，没带或版本号对不上的照旧不缓存。"""

import hashlib
import os
import re

from fastapi.testclient import TestClient

from researchlib.config import Settings
from researchlib.web import assets
from researchlib.web.app import create_app
from researchlib.web.assets import IMMUTABLE, asset_url, asset_version
from researchlib.web.routes import TEMPLATES


def test_versioned_static_assets_are_cached_and_the_rest_are_not(tmp_path):
    url = asset_url("site.css")
    version = hashlib.sha256((assets.STATIC_DIR / "site.css").read_bytes()).hexdigest()[:12]
    assert url == f"/static/site.css?v={version}"
    client = TestClient(create_app(Settings.from_env({"DATA_DIR": str(tmp_path)})))

    cached = client.get(url)
    assert cached.status_code == 200
    assert cached.headers["cache-control"] == IMMUTABLE
    assert cached.headers["x-content-type-options"] == "nosniff"
    again = client.get(url, headers={"If-None-Match": cached.headers["etag"]})
    assert again.status_code == 304 and again.headers["cache-control"] == IMMUTABLE

    for stale in ("/static/site.css", "/static/site.css?v=0123456789ab", f"/static/article.js?v={version}"):
        response = client.get(stale)
        assert response.status_code == 200, stale
        assert response.headers["cache-control"] == "private, no-store", stale
    assert client.get("/static/missing.css?v=0123456789ab").headers["cache-control"] == "private, no-store"


def test_version_follows_the_file_and_stays_inside_static(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "STATIC_DIR", tmp_path)
    (tmp_path / "a.css").write_text("body{color:red}", encoding="utf-8")
    before = asset_version("a.css")
    (tmp_path / "a.css").write_text("body{color:blue}", encoding="utf-8")
    os.utime(tmp_path / "a.css", ns=(1, 1))
    after = asset_version("a.css")
    assert before and after and before != after
    assert asset_version("../a.css") == "" and asset_version("missing.css") == ""
    assert asset_url("missing.css") == "/static/missing.css"


def test_pages_link_styles_and_scripts_with_their_version():
    page = TEMPLATES.env.get_template("404.html").render(
        title="没有这一页", nav="", query="", public_host="investment.zldong.com",
        canonical="https://investment.zldong.com/x", sample_banner=False, sources=[], active_source="",
    )
    assert re.search(r'href="/static/site\.css\?v=[0-9a-f]{12}"', page)
    templates = assets.STATIC_DIR.parent / "templates"
    for name in ("base.html", "article.html", "ask.html"):
        text = (templates / name).read_text(encoding="utf-8")
        assert not re.search(r'(?:href|src)="/static/', text), name


def test_every_response_says_how_long_the_server_took(tmp_path):
    # 浏览器开发者工具和 curl 都看得到这一项，和总耗时一比就知道慢在服务器上还是路上。
    client = TestClient(create_app(Settings.from_env({"DATA_DIR": str(tmp_path)})))
    for path in (asset_url("site.css"), "/static/missing.css"):
        timing = client.get(path).headers["server-timing"]
        assert re.fullmatch(r"app;dur=\d+\.\d", timing), (path, timing)


def test_pages_and_assets_are_compressed_but_images_are_not(tmp_path):
    # 慢在 Mac mini 经隧道到 Cloudflare 那一段，传压缩后的字节（审核 O1）。
    client = TestClient(create_app(Settings.from_env({"DATA_DIR": str(tmp_path)})))
    plain = client.get(asset_url("site.css"), headers={"Accept-Encoding": "identity"})
    packed = client.get(asset_url("site.css"), headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in plain.headers
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.content == plain.content  # httpx 自己解压
    assert packed.num_bytes_downloaded < len(plain.content) / 3
    assert packed.headers["cache-control"] == IMMUTABLE and packed.headers["server-timing"]
    # 小于 1000 字节的不压。
    assert "content-encoding" not in client.get("/robots.txt", headers={"Accept-Encoding": "gzip"}).headers


def test_favicon_is_cached_for_a_day(tmp_path):
    client = TestClient(create_app(Settings.from_env({"DATA_DIR": str(tmp_path)})))
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_only_content_hashed_media_names_count_as_immutable():
    digest = "0123456789abcdef" * 4
    assert assets.hashed_media(f"remote/01/{digest}.png")
    assert assets.hashed_media(f"fangwei-mp/01/{digest}.jpeg")
    for name in ("audit-a/a.png", f"remote/01/{digest}", f"remote/01/{digest[:-1]}.png",
                 f"remote/01/{digest.upper()}.png", f"remote/01/{digest}.png/x", ""):
        assert not assets.hashed_media(name), name

"""公众号正文：显示时去掉归档头、二维码和文末招募，库里的原文不动。"""

from datetime import datetime

from fastapi.testclient import TestClient

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI
from researchlib.web.app import create_app

BODY = """
# 渠道怎么看

作者：方伟
时间：2026-10-14 07:00:00
原文链接：https://mp.weixin.qq.com/s/abc
阅读：1234

腾讯的渠道优势是零下载成本，能快速转化存量用户。这段要留在正文里，讲的是商业模式而不是文末号召。

![](chart.png)

再补一句：广告库存还在抬升，不是文末号召。

适合认可巴菲特、
价值投资理念的朋友加入，
我们只在能力圈范围内讨论企业的商业模式

## 招募

长按识别二维码关注公众号

加微信：example_id
所以白嫖勿扰。

![](images/qrcode.jpg)
"""


def test_rendered_mp_article_drops_qr_and_recruiting(conn, settings):
    source = next(item for item in load_sources(settings.sources_path) if item.id == "fangwei-mp")
    store_documents(
        conn,
        source=source,
        docs=[
            ParsedDoc(
                external_id="mp-cta-demo",
                title="渠道怎么看",
                published_at=datetime(2026, 10, 14, 7, 0, tzinfo=SHANGHAI),
                body_md=BODY,
                raw_meta={"format": "markdown"},
                image_refs=[],
            )
        ],
        data_dir=settings.data_dir,
        archive_files=1,
        images_in_archive=0,
        errors=[],
    )
    row = conn.execute(
        "SELECT id, body_md FROM articles WHERE external_id = %s",
        ("mp-cta-demo",),
    ).fetchone()
    assert "qrcode.jpg" in row["body_md"] and "招募" in row["body_md"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/articles/{row['id']}")
    assert page.status_code == 200
    html = page.text
    assert "渠道优势" in html and "chart.png" in html and "广告库存还在抬升" in html
    prose = html.split('class="prose"', 1)[1].split("</article>", 1)[0]
    for word in ("作者：", "mp.weixin.qq.com", "招募", "长按识别", "qrcode", "加微信", "白嫖", "能力圈"):
        assert word not in prose

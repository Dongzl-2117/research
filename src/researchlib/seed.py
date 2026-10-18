"""写入明显标成示例的文章，方便看页面。不会碰付费原文。"""

from __future__ import annotations

import os
import struct
import sys
import zlib
from datetime import datetime
from pathlib import Path

from researchlib.config import Settings
from researchlib.db import prepare
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import SHANGHAI


def write_sample_png(path: Path) -> None:
    width, height = 640, 280

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            bar = 48 < y < 230 and (x // 36) % 2 == 0 and y > 80 + (x % 90)
            raw.extend((143, 45, 45) if bar else (251, 247, 241))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _doc(external_id: str, title: str, when: datetime, body: str, image: tuple[str, Path] | None = None) -> ParsedDoc:
    refs = [image] if image else []
    return ParsedDoc(
        external_id=external_id,
        title=title,
        published_at=when,
        body_md=body.strip() + "\n",
        raw_meta={"format": "markdown", "sample": "true", "parent": ""},
        image_refs=refs,
    )


def sample_documents(image: Path) -> dict[str, list[ParsedDoc]]:
    return {
        "fangwei-mp": [
            _doc(
                "sample:tencent-ads",
                "腾讯：广告与视频号的两笔账（示例）",
                datetime(2024, 3, 12, tzinfo=SHANGHAI),
                """
# 腾讯：广告与视频号的两笔账（示例）

这是种子数据，不是付费原文。

## 广告

腾讯的广告业务在这篇示例里被当作主体。腾讯视频号和腾讯金融科技各写一层，用来演示标题深度。

## 顺带

美团在这里只出现一次，用来演示一带而过。
""",
            ),
            _doc(
                "sample:moutai-channel",
                "贵州茅台的渠道库存（示例）",
                datetime(2024, 6, 2, tzinfo=SHANGHAI),
                """
# 贵州茅台的渠道库存（示例）

## 库存

茅台的批价只是示例数字，不要当成研究结论。

## 同业

五粮液的动销在这一节里写了两遍。五粮液的渠道库存也只是占位。
""",
            ),
            _doc(
                "sample:apple-chain",
                "苹果链的冷暖（示例）",
                datetime(2024, 11, 20, tzinfo=SHANGHAI),
                """
# 苹果链的冷暖（示例）

## 组装

立讯精密给苹果链供货这件事写两句。立讯精密的毛利率没有真实数字。

## 别的公司

亚马逊在这一句里被顺带提到。
""",
            ),
            _doc(
                "sample:battery-note",
                "年初备忘：电池（示例）",
                datetime(2026, 1, 15, 8, 30, tzinfo=SHANGHAI),
                """
# 年初备忘：电池（示例）

## 产能

宁德时代的产能利用率先空着。

## 库存

宁德时代的库存和宁德时代的客户都还没写完。

## 顺带

腾讯这里只点一次名。

附图是示例图，不是真实数据。

![](battery.png)
""",
                ("battery.png", image),
            ),
            _doc(
                "sample:evening-note",
                "晚间短记：仓位（示例）",
                datetime(2026, 1, 15, 21, 40, tzinfo=SHANGHAI),
                """
# 晚间短记：仓位（示例）

这则只记仓位和节奏，不点名。示例，不是原文。
""",
            ),
            _doc(
                "sample:morning-note",
                "晨间短记：节奏（示例）",
                datetime(2026, 1, 15, 9, 5, tzinfo=SHANGHAI),
                """
# 晨间短记：节奏（示例）

早上先写节奏，同样不点名。示例，不是原文。
""",
            ),
        ],
        "fangwei-zsxq": [
            _doc(
                "sample:pdd-note",
                "星球短帖：平台竞争（示例）",
                datetime(2025, 1, 8, tzinfo=SHANGHAI),
                """
平台竞争这则短帖是示例。拼多多的商家广告还在变贵。拼多多把补贴压在供应链上。拼多多和传统货架的差别先记在这里。字节只在句尾提一次。
""",
            ),
            _doc(
                "sample:meituan-unit",
                "美团外卖的单位经济（示例）",
                datetime(2025, 4, 18, tzinfo=SHANGHAI),
                """
# 美团外卖的单位经济（示例）

## 单均

美团这节只是占位，没有真实 UE。

## 股东

腾讯作为股东在这一节出现。腾讯的持股比例不引用任何付费原文。
""",
            ),
        ],
        "chengfengpolang": [
            _doc(
                "sample:amazon-cloud",
                "亚马逊云和广告（示例）",
                datetime(2025, 2, 14, tzinfo=SHANGHAI),
                """
# 亚马逊云和广告（示例）

## 云

微软的云业务在这一节写两句。微软的资本开支没有真实数字。

## 其他

谷歌只被提到一次。
""",
            ),
            _doc(
                "sample:quarter-notes",
                "季度流水账（示例）",
                datetime(2025, 7, 1, tzinfo=SHANGHAI),
                """
# 季度流水账（示例）

## 汽车

比亚迪出货。比亚迪价格。都是空话，用来占住「单节」。

## 酒

茅台两个字带过。
""",
            ),
            _doc(
                "sample:bytedance-org",
                "字节跳动的组织（示例）",
                datetime(2025, 9, 9, tzinfo=SHANGHAI),
                """
# 字节跳动的组织（示例）

字节跳动的业务线在示例里重复一遍。字节跳动没有公开财报，这里也不编造数字。
""",
            ),
        ],
    }


def load_sample(conn, settings: Settings):
    image = settings.data_dir / "sample" / "battery.png"
    write_sample_png(image)
    sources = {source.id: source for source in load_sources(settings.sources_path)}
    reports = []
    for source_id, docs in sample_documents(image).items():
        source = sources.get(source_id)
        if source is None or not source.enabled:
            raise SystemExit(f"{source_id} 未启用，不能写入示例")
        reports.append(
            store_documents(
                conn,
                source=source,
                docs=docs,
                data_dir=settings.data_dir,
                archive_files=len(docs),
                images_in_archive=1 if source_id == "fangwei-mp" else 0,
                errors=[],
            )
        )
    return reports


def main(argv: list[str] | None = None) -> int:
    if argv:
        raise SystemExit("research-seed-sample 不接受参数")
    if os.environ.get("ALLOW_SAMPLE_SEED") != "1":
        print("拒绝写入示例。确认这不是生产库后，设置 ALLOW_SAMPLE_SEED=1。", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    conn = prepare(settings)
    try:
        reports = load_sample(conn, settings)
    finally:
        conn.close()
    for report in reports:
        print(report.render())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

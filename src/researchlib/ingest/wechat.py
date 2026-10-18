"""微信公众号 Markdown 归档。目录或 zip 都可以，图片包按文件名回退匹配。"""

from __future__ import annotations

import sys

from researchlib.ingest.cli import run_markdown_ingest


def main(argv: list[str] | None = None) -> int:
    return run_markdown_ingest(
        "wechat_mp",
        "导入微信公众号 Markdown 归档（可含 01-text.zip 和图片 zip）",
        argv,
        id_mode="path",
    )


if __name__ == "__main__":
    sys.exit(main())

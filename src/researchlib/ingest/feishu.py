"""飞书导出的 Markdown。目录可以嵌套，外部 id 用相对路径。"""

from __future__ import annotations

import sys

from researchlib.ingest.cli import run_markdown_ingest


def main(argv: list[str] | None = None) -> int:
    return run_markdown_ingest(
        "feishu",
        "导入 feishu2md 风格的嵌套 Markdown",
        argv,
        id_mode="path",
    )


if __name__ == "__main__":
    sys.exit(main())

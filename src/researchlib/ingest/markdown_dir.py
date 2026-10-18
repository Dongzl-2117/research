"""通用 Markdown 目录。孤独进化体启用后走这个导入器。"""

from __future__ import annotations

import sys

from researchlib.ingest.cli import run_markdown_ingest


def main(argv: list[str] | None = None) -> int:
    return run_markdown_ingest(
        "markdown",
        "导入通用 Markdown 目录",
        argv,
        id_mode="path",
    )


if __name__ == "__main__":
    sys.exit(main())

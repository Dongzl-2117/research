"""知识星球归档。优先读 md/、images/、raw/ 里的话题 JSON。"""

from __future__ import annotations

import sys

from researchlib.ingest.cli import run_markdown_ingest


def main(argv: list[str] | None = None) -> int:
    return run_markdown_ingest(
        "zsxq",
        "导入知识星球归档（md/、images/、raw/）",
        argv,
        id_mode="zsxq",
    )


if __name__ == "__main__":
    sys.exit(main())

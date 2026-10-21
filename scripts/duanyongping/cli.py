#!/usr/bin/env python3
"""段永平博客 PDF 导入。实现在 researchlib.ingest.duanyongping，这里方便直接跑。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from researchlib.ingest.duanyongping.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

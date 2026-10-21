#!/usr/bin/env python3
"""雪球「黑黑无常」已抓取 JSON → Markdown。实现在 researchlib.ingest.xueqiu。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from researchlib.ingest.xueqiu import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

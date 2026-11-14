"""两个 Dockerfile 的底座按 digest 钉住（docs/deploy.md「基础镜像」）。

只写标签时，上游一重推，下一次自动上线就在 Mac mini 上重拉底座、整个重建：11-13 数据库镜像建了 3 小时 20 分。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PINNED = re.compile(r"FROM\s+[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}(\s+AS\s+\w+)?\s*")


@pytest.mark.parametrize("path", ["Dockerfile", "docker/postgres/Dockerfile"])
def test_base_image_is_pinned_by_digest(path):
    lines = [line for line in (ROOT / path).read_text(encoding="utf-8").splitlines() if line.startswith("FROM ")]
    assert lines
    for line in lines:
        assert PINNED.fullmatch(line), line

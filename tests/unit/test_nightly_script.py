"""scripts/nightly.sh：一次性数据修复每条只成功跑一次，失败的下一晚再试；之后的补齐步骤照常跑。

PATH 里放假的 docker，记下 compose exec 进 app 容器跑了什么；NIGHTLY_FAIL 里写的命令假装失败。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="要 bash")

FAKE_DOCKER = """#!/bin/bash
case "$*" in
  *" ps --status running --services")
    echo app
    ;;
  *" exec -T app true")
    ;;
  *" exec -T app "*)
    all="$*"
    command="${all#* exec -T app }"
    echo "$command" >> "$CALLS"
    case "$command" in
      "${NIGHTLY_FAIL:-没有要失败的}"*)
        echo "库连不上" >&2
        exit 3
        ;;
    esac
    ;;
esac
"""

ONCE = [
    "research-retime --source chengfengpolang",
    "research-repair-zsxq --source chengfengpolang",
    "research-clean --source chengfengpolang --source fangwei-zsxq",
    "research-rebuild-chunks --ocr",
]


class Mini:
    def __init__(self, tmp: Path):
        self.repo = tmp / "research"
        for name in ("nightly.sh", "status.sh"):
            target = self.repo / "scripts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / "scripts" / name, target)
        (self.repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.calls = tmp / "calls.log"

    def night(self, **env: str) -> list[str]:
        self.calls.write_text("", encoding="utf-8")
        full = {**os.environ, "PATH": self.path, "REPO": str(self.repo), "CALLS": str(self.calls), **env}
        subprocess.run(
            ["bash", str(self.repo / "scripts" / "nightly.sh")], env=full, capture_output=True, text=True
        )
        return self.calls.read_text(encoding="utf-8").splitlines()

    def status(self) -> dict:
        return json.loads((self.repo / "data" / "status" / "nightly.json").read_text(encoding="utf-8"))

    def marks(self) -> list[str]:
        folder = self.repo / "data" / "status" / "once"
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def test_one_off_repairs_run_once_before_the_nightly_steps(tmp_path: Path):
    mini = Mini(tmp_path)
    first = mini.night()
    assert first[: len(ONCE)] == ONCE
    assert any(call.startswith("research-embed") for call in first[len(ONCE) :])
    assert len(mini.marks()) == len(ONCE)
    assert mini.status()["ok"] is True
    assert mini.status()["message"].startswith("一次性修复 2026-10-29-retime-chengfengpolang、")
    assert "2026-10-30-rebuild-ocr-chunks；外链图片" in mini.status()["message"]

    second = mini.night()
    assert not set(ONCE) & set(second)
    assert any(call.startswith("research-digest") for call in second)
    assert mini.status()["ok"] is True
    assert "一次性修复" not in mini.status()["message"]


def test_failed_repair_is_reported_and_retried_next_night(tmp_path: Path):
    mini = Mini(tmp_path)
    calls = mini.night(NIGHTLY_FAIL="research-repair-zsxq")
    # 后面的修复和补齐步骤照常跑。
    assert calls[: len(ONCE)] == ONCE
    assert any(call.startswith("research-views") for call in calls)
    assert "2026-10-30-repair-chengfengpolang" not in mini.marks()
    assert len(mini.marks()) == len(ONCE) - 1
    status = mini.status()
    assert status["ok"] is False
    assert "一次性修复 2026-10-30-repair-chengfengpolang（退出码 3：库连不上）" in status["message"]

    retried = mini.night()
    assert [call for call in retried if call in ONCE] == ["research-repair-zsxq --source chengfengpolang"]
    assert len(mini.marks()) == len(ONCE)
    assert mini.status()["ok"] is True

"""scripts/install-launchd.sh：装哪些任务、设置记下来以后重跑不用再写、自动上线重装时不停掉自己。

PATH 里放假的 launchctl、plutil 和 docker，HOME 指到临时目录，记下 launchctl 被调用了什么。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="要 bash")

FAKE_LAUNCHCTL = """#!/bin/bash
echo "launchctl $1 $(basename "${3:-}")" >> "$CALLS"
"""


class Mac:
    def __init__(self, tmp: Path):
        self.repo = tmp / "research"
        self.home = tmp / "home"
        self.calls = tmp / "calls.log"
        shutil.copytree(ROOT / "deploy" / "launchd", self.repo / "deploy" / "launchd")
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "install-launchd.sh", self.repo / "scripts" / "install-launchd.sh")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("launchctl", FAKE_LAUNCHCTL), ("plutil", "#!/bin/bash\n"), ("docker", "#!/bin/bash\n")):
            (bin_dir / name).write_text(body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def install(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        self.calls.write_text("", encoding="utf-8")
        full = {**os.environ, "PATH": self.path, "HOME": str(self.home), "CALLS": str(self.calls), **env}
        for name in ("BACKUP_DIR", "INSTALL_DEPLOY", "INSTALL_NIGHTLY", "INSTALL_CRAWL", "NIGHTLY_OCR", "DEPLOY_SELF"):
            if name not in env:
                full.pop(name, None)
        script = self.repo / "scripts" / "install-launchd.sh"
        return subprocess.run(["bash", str(script), *args], env=full, capture_output=True, text=True)

    def called(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()

    def agent(self, name: str) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"com.zldong.research.{name}.plist"


def test_settings_are_remembered_and_deploy_does_not_stop_itself(tmp_path: Path):
    mac = Mac(tmp_path)
    backups = tmp_path / "备份 目录"
    assert mac.install().returncode == 2
    result = mac.install(BACKUP_DIR=str(backups), NIGHTLY_OCR="1")
    assert result.returncode == 0, result.stderr
    assert "launchctl bootstrap com.zldong.research.deploy.plist" in mac.called()
    assert str(backups) in mac.agent("backup").read_text(encoding="utf-8")
    assert "<string>1</string>" in mac.agent("nightly").read_text(encoding="utf-8")

    # 第二次什么都不写：备份目录、夜间识别图片文字都照上次的。
    mac.agent("backup").unlink()
    result = mac.install()
    assert result.returncode == 0, result.stderr
    assert str(backups) in mac.agent("backup").read_text(encoding="utf-8")
    assert "<string>1</string>" in mac.agent("nightly").read_text(encoding="utf-8")

    # 自动上线拉到新模板后重装：别的任务重新装载，自动上线自己只写新文件。
    result = mac.install(DEPLOY_SELF="1")
    assert result.returncode == 0, result.stderr
    assert "launchctl bootstrap com.zldong.research.nightly.plist" in mac.called()
    assert not any(call.endswith("research.deploy.plist") for call in mac.called())
    assert mac.agent("deploy").exists()


def test_turning_deploy_off_sticks(tmp_path: Path):
    mac = Mac(tmp_path)
    backups = str(tmp_path / "backups")
    assert mac.install(BACKUP_DIR=backups).returncode == 0
    status = mac.repo / "data" / "status"
    status.mkdir(parents=True)
    (status / "deploy.json").write_text("{}", encoding="utf-8")
    assert mac.install(INSTALL_DEPLOY="0").returncode == 0
    assert "launchctl bootout com.zldong.research.deploy.plist" in mac.called()
    assert not mac.agent("deploy").exists() and not (status / "deploy.json").exists()
    assert mac.install().returncode == 0
    assert not mac.agent("deploy").exists()

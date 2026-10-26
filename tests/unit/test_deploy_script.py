"""scripts/deploy.sh：main 上有 CI 通过的新提交才拉代码重建，结果写进 data/status/deploy.json。

拿本地的裸仓库当 GitHub 上的 origin，PATH 里放假的 docker 和 curl，记下它们被调用了什么。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None or shutil.which("python3") is None,
    reason="要 bash、git 和 python3",
)

FAKE_DOCKER = """#!/bin/bash
echo "docker $*" >> "$CALLS"
if [ "${DOCKER_FAILS:-0}" = "1" ]; then
  echo "镜像没建成" >&2
  exit 1
fi
"""
# GitHub 接口返回测试写好的 CI 结果；没写就当接口出错。应用的健康检查一律通过。
FAKE_CURL = """#!/bin/bash
case "$*" in
  *api.github.com*)
    echo "curl ci" >> "$CALLS"
    [ -f "$CI_JSON" ] || exit 22
    cat "$CI_JSON"
    ;;
  *)
    echo "curl app" >> "$CALLS"
    ;;
esac
"""
FAKE_INSTALL = """#!/bin/bash
echo "install DEPLOY_SELF=${DEPLOY_SELF:-}" >> "$CALLS"
"""


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=测试", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


class World:
    """origin（裸仓库）、开发机上的工作区、Mac mini 上的仓库，和假的 docker、curl。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.mini = tmp / "mini"
        self.calls = tmp / "calls.log"
        self.ci = tmp / "ci.json"
        bin_dir = tmp / "bin"
        _write(bin_dir / "docker", FAKE_DOCKER, 0o755)
        _write(bin_dir / "curl", FAKE_CURL, 0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(self.origin)], check=True)
        _git(tmp, "clone", "--quiet", str(self.origin), str(self.work))
        _git(self.work, "checkout", "--quiet", "-B", "main")
        for name in ("deploy.sh", "status.sh"):
            _write(self.work / "scripts" / name, (ROOT / "scripts" / name).read_text(encoding="utf-8"), 0o755)
        _write(self.work / "scripts" / "install-launchd.sh", FAKE_INSTALL, 0o755)
        _write(self.work / "docker-compose.yml", "services: {}\n")
        _write(self.work / "deploy" / "launchd" / "com.example.plist", "<plist/>\n")
        self.commit("第一版")
        _git(tmp, "clone", "--quiet", "-b", "main", str(self.origin), str(self.mini))

    def commit(self, subject: str, path: str = "notes.txt") -> str:
        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(subject + "\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "--quiet", "-m", subject)
        _git(self.work, "push", "--quiet", "origin", "main")
        return _git(self.work, "rev-parse", "HEAD")

    def ci_says(self, status: str, conclusion: str | None = None) -> None:
        runs = [{"id": 1, "name": "Tests", "status": status, "conclusion": conclusion}]
        self.ci.write_text(json.dumps({"total_count": 1, "check_runs": runs}), encoding="utf-8")

    def deploy(self, **env: str) -> subprocess.CompletedProcess:
        self.calls.write_text("", encoding="utf-8")
        full = {
            **os.environ,
            "PATH": self.path,
            "REPO": str(self.mini),
            "CALLS": str(self.calls),
            "CI_JSON": str(self.ci),
            "DEPLOY_REPO": "example/research",
            "APP_URL": "http://app.test/health",
            **env,
        }
        return subprocess.run(
            ["bash", str(self.mini / "scripts" / "deploy.sh")], env=full, capture_output=True, text=True
        )

    def called(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()

    def status(self) -> dict | None:
        path = self.mini / "data" / "status" / "deploy.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def head(self) -> str:
        return _git(self.mini, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_green_commit_goes_live_once(world: World):
    world.ci_says("completed", "success")
    sha = world.commit("把标题补全")
    result = world.deploy()
    assert result.returncode == 0, result.stderr
    assert world.head() == sha
    assert any(call.startswith("docker compose") and call.endswith("up -d --build") for call in world.called())
    status = world.status()
    assert status["ok"] is True and status["message"] == f"上线 {sha[:7]}：把标题补全"
    # 没有新提交：不查 CI，不重建。
    assert world.deploy().returncode == 0
    assert world.called() == []


def test_waits_for_ci_and_does_not_ship_a_red_commit(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    shipped = world.head()
    world.commit("改坏了")
    world.ci_says("in_progress")
    assert world.deploy().returncode == 0
    assert world.called() == ["curl ci"] and world.head() == shipped
    # 被后来的推送取消的那次不算没过。
    world.ci_says("completed", "cancelled")
    assert world.deploy().returncode == 0
    assert world.head() == shipped and world.status()["ok"] is True
    world.ci_says("completed", "failure")
    assert world.deploy().returncode == 0
    assert world.head() == shipped
    status = world.status()
    assert status["ok"] is False and "CI 没通过，没有上线" in status["message"]
    # CI 重跑通过了：这一轮上线，提示消失。
    world.ci_says("completed", "success")
    assert world.deploy().returncode == 0
    assert world.head() != shipped and world.status()["ok"] is True


def test_failed_build_is_retried_three_times_then_left_alone(world: World):
    world.ci_says("completed", "success")
    world.commit("新功能")
    for _ in range(3):
        result = world.deploy(DOCKER_FAILS="1")
        assert result.returncode != 0
        status = world.status()
        assert status["ok"] is False and status["message"].startswith("重建并重启失败")
        assert "镜像没建成" in status["message"]
    assert world.deploy(DOCKER_FAILS="1").returncode == 0
    assert world.called() == []
    # 手动 FORCE=1 照样重试。
    assert world.deploy(FORCE="1").returncode == 0
    assert world.status()["ok"] is True


def test_launchd_templates_changed_means_reinstall_without_stopping_itself(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    world.commit("加一个定时任务", path="deploy/launchd/com.example.plist")
    assert world.deploy().returncode == 0
    assert "install DEPLOY_SELF=1" in world.called()
    world.commit("只改文档")
    assert world.deploy().returncode == 0
    assert not any(call.startswith("install") for call in world.called())


def test_unreachable_github_is_quiet_unless_it_never_worked(world: World):
    world.commit("第二版")
    # 装好以后从没查到过 CI：直接提示。
    assert world.deploy().returncode == 0
    status = world.status()
    assert status["ok"] is False and "查不到" in status["message"]
    world.ci_says("completed", "success")
    assert world.deploy().returncode == 0 and world.status()["ok"] is True
    # 刚成功过：偶尔一次查不到不提示。
    world.commit("第三版")
    world.ci.unlink()
    assert world.deploy().returncode == 0
    assert world.status()["ok"] is True
    assert world.head() != _git(world.work, "rev-parse", "HEAD")


def test_waits_for_jobs_running_in_the_containers(world: World):
    world.ci_says("completed", "success")
    sha = world.commit("第二版")
    nightly = world.tmp / "elsewhere" / "scripts" / "nightly.sh"
    _write(nightly, "while :; do sleep 1; done\n", 0o755)
    running = subprocess.Popen(["bash", str(nightly)])
    try:
        assert world.deploy().returncode == 0
        assert world.called() == [] and world.status() is None
    finally:
        running.kill()
        running.wait()
    assert world.deploy().returncode == 0
    assert world.head() == sha and world.status()["ok"] is True


def test_local_edits_on_the_mini_are_not_shipped(world: World):
    world.ci_says("completed", "success")
    world.commit("第二版")
    with (world.mini / "docker-compose.yml").open("a", encoding="utf-8") as handle:
        handle.write("# 本机改的\n")
    assert world.deploy().returncode == 0
    status = world.status()
    assert status["ok"] is False and "docker-compose.yml" in status["message"]
    assert world.called() == []
    _git(world.mini, "checkout", "--", "docker-compose.yml")
    assert world.deploy().returncode == 0 and world.status()["ok"] is True


def test_long_outage_is_reported_and_cleared_once_main_is_back(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    away = world.tmp / "away.git"
    world.origin.rename(away)
    # 刚成功过：断一轮不提示。
    assert world.deploy().returncode == 0 and world.status()["ok"] is True
    seen = world.mini / "data" / "status" / ".deploy-seen"
    long_ago = seen.stat().st_mtime - 7 * 3600
    os.utime(seen, (long_ago, long_ago))
    assert world.deploy().returncode == 0
    status = world.status()
    assert status["ok"] is False and "取不到 origin/main" in status["message"]
    # 连上了，main 上没有新提交：不重建，提示消失。
    away.rename(world.origin)
    assert world.deploy().returncode == 0
    assert world.status()["ok"] is True and world.called() == []


def test_checkout_on_another_branch_is_not_touched(world: World):
    world.ci_says("completed", "success")
    _git(world.mini, "checkout", "--quiet", "-b", "试验")
    world.commit("第二版")
    assert world.deploy().returncode == 0
    status = world.status()
    assert status["ok"] is False and "不在 main" in status["message"]
    assert not any(call.startswith("docker") for call in world.called())

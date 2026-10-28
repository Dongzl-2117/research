"""scripts/deploy.sh：main 上有 CI 通过的新提交才拉代码重建，结果写进 data/status/deploy.json。

拿本地的裸仓库当 GitHub 上的 origin，PATH 里放假的 docker 和 curl，记下它们被调用了什么。
假的 docker 建镜像时把传进来的 GIT_SHA 记成「在跑的提交」，假的 /health 报出它。
"""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None or shutil.which("python3") is None,
    reason="要 bash、git 和 python3",
)

# STUCK=1：compose 没换容器，旧的接着跑。
FAKE_DOCKER = """#!/bin/bash
echo "docker $*" >> "$CALLS"
if [ "${DOCKER_FAILS:-0}" = "1" ]; then
  echo "镜像没建成" >&2
  exit 1
fi
case "$*" in
  *"up -d --build"*)
    if [ "${STUCK:-0}" != "1" ]; then
      echo "${GIT_SHA:-unknown}" > "$RUNNING"
    fi
    ;;
esac
"""
# GitHub 接口返回测试写好的 CI 结果；没写就当接口出错。
# 应用的健康检查报出在跑的提交；在跑的提交列在 BROKEN 里（空格隔开）就当应用没起来。
FAKE_CURL = """#!/bin/bash
case "$*" in
  *api.github.com*)
    echo "curl ci" >> "$CALLS"
    [ -f "$CI_JSON" ] || exit 22
    cat "$CI_JSON"
    ;;
  *)
    echo "curl app" >> "$CALLS"
    running="$(cat "$RUNNING" 2> /dev/null || echo unknown)"
    case " ${BROKEN:-} " in
      *" $running "*) exit 7 ;;
    esac
    printf '{"status":"ok","commit":"%s"}' "$running"
    ;;
esac
"""
FAKE_INSTALL = """#!/bin/bash
echo "install DEPLOY_SELF=${DEPLOY_SELF:-}" >> "$CALLS"
"""
FAKE_BACKUP = """#!/bin/bash
echo "backup BACKUP_DIR=${BACKUP_DIR:-}" >> "$CALLS"
if [ "${BACKUP_FAILS:-0}" = "1" ]; then
  echo "pg_dump 出错" >&2
  exit 1
fi
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
        self.serving = tmp / "running.txt"
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
        _write(self.work / "scripts" / "backup.sh", FAKE_BACKUP, 0o755)
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

    def save_settings(self, backup_dir: Path) -> None:
        """install-launchd.sh 记下的设置（data/launchd.env）。"""
        _write(self.mini / "data" / "launchd.env", f"SAVED_BACKUP_DIR={shlex.quote(str(backup_dir))}\n")

    def deploy(self, **env: str) -> subprocess.CompletedProcess:
        self.calls.write_text("", encoding="utf-8")
        full = {
            **{key: value for key, value in os.environ.items() if key not in ("BACKUP_DIR", "GIT_SHA")},
            "PATH": self.path,
            "REPO": str(self.mini),
            "CALLS": str(self.calls),
            "CI_JSON": str(self.ci),
            "RUNNING": str(self.serving),
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

    def running(self) -> str:
        return self.serving.read_text(encoding="utf-8").strip() if self.serving.exists() else "unknown"


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
    # 镜像带着这次的提交号，/health 报出它才算上线；上线后清掉这个项目换下来的旧镜像。
    assert world.running() == sha
    assert world.called()[-1] == "docker image prune -f --filter label=com.zldong.research"
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
    first = world.head()
    world.commit("新功能")
    for _ in range(3):
        result = world.deploy(DOCKER_FAILS="1")
        assert result.returncode != 0
        status = world.status()
        assert status["ok"] is False and status["message"].startswith("重建并重启失败")
        assert "镜像没建成" in status["message"]
        # 仓库退回去，和还在跑的旧容器对得上。
        assert world.head() == first and "线上照常" in status["message"]
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


def test_unpushed_commits_on_the_mini_are_not_shipped(world: World):
    world.ci_says("completed", "success")
    # 本机 main 比 origin 多一个提交：merge --ff-only 不报错，不拦的话这个提交会被打进镜像。
    _write(world.mini / "notes.txt", "本机改的\n")
    _git(world.mini, "commit", "--quiet", "-am", "本机提交")
    assert world.deploy().returncode == 0
    status = world.status()
    assert status["ok"] is False and "1 个没推送的提交" in status["message"]
    assert world.called() == []
    # 存到分支，main 回到 origin/main：之后的提交照常上线。
    _git(world.mini, "branch", "mini-local")
    _git(world.mini, "reset", "--quiet", "--keep", "origin/main")
    sha = world.commit("第二版")
    assert world.deploy().returncode == 0
    assert world.head() == sha and world.status()["ok"] is True


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


def test_broken_release_goes_back_to_the_previous_commit(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    good = world.head()
    bad = world.commit("启动就崩")
    result = world.deploy(BROKEN=bad, DEPLOY_WAIT="0")
    assert result.returncode != 0
    # 仓库退回上线前的提交，按它重建，线上回到原来的版本。
    assert world.head() == good and world.running() == good
    status = world.status()
    assert status["ok"] is False
    assert f"/health 都没报出 {bad[:7]}" in status["message"]
    assert f"已退回 {good[:7]}，线上照常" in status["message"]
    # 退回去会换掉起不来的那个容器，它的日志先存下来。
    calls = world.called()
    assert "docker compose --project-directory" in calls[calls.index("curl app") + 1]
    assert calls[calls.index("curl app") + 1].endswith("logs --no-color --tail 300 app")
    assert (world.mini / "data" / "status" / "deploy-failed.log").exists()
    assert not any("image prune" in call for call in calls)
    # 起不来的不自动重试：下一轮什么都不做，提示留着。
    assert world.deploy(BROKEN=bad, DEPLOY_WAIT="0").returncode == 0
    assert world.called() == [] and world.status()["ok"] is False
    # 修好的下一个提交照常上线。
    fixed = world.commit("修好了")
    assert world.deploy(BROKEN=bad).returncode == 0
    assert world.head() == fixed and world.running() == fixed and world.status()["ok"] is True


def test_old_container_still_answering_is_not_taken_for_the_new_one(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    good = world.head()
    new = world.commit("第二版")
    # compose 没换容器：/health 通，报的却是旧提交。
    result = world.deploy(STUCK="1", DEPLOY_WAIT="0")
    assert result.returncode != 0
    assert world.head() == good
    status = world.status()
    assert status["ok"] is False and f"没报出 {new[:7]}" in status["message"]


def test_says_so_when_even_the_old_version_does_not_come_back(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    good = world.head()
    bad = world.commit("改坏了")
    result = world.deploy(BROKEN=f"{bad} {good}", DEPLOY_WAIT="0")
    assert result.returncode != 0
    assert world.head() == good
    status = world.status()
    assert status["ok"] is False and "应用还是没起来，线上停着" in status["message"]


def test_new_migrations_are_backed_up_before_going_live(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    backups = world.tmp / "备份 盘"
    world.save_settings(backups)
    sha = world.commit("加一列", path="src/researchlib/migrations/025_add_column.sql")
    assert world.deploy().returncode == 0
    calls = world.called()
    backup_at = calls.index(f"backup BACKUP_DIR={backups}")
    build_at = next(index for index, call in enumerate(calls) if call.endswith("up -d --build"))
    assert backup_at < build_at
    assert world.head() == sha and world.status()["ok"] is True
    # 不带迁移的提交不备份。
    world.commit("只改页面", path="src/researchlib/web/page.txt")
    assert world.deploy().returncode == 0
    assert not any(call.startswith("backup") for call in world.called())
    assert world.status()["ok"] is True


def test_no_release_with_migrations_until_a_backup_is_made(world: World):
    world.ci_says("completed", "success")
    world.deploy()
    good = world.head()
    world.commit("删一列", path="src/researchlib/migrations/025_drop_column.sql")
    # 不知道备份放哪：不上线，每轮再看。
    assert world.deploy().returncode == 0
    status = world.status()
    assert status["ok"] is False and "找不到备份目录" in status["message"]
    assert world.head() == good and not any(call.startswith("docker") for call in world.called())
    # 备份失败：不上线。
    world.save_settings(world.tmp / "backups")
    assert world.deploy(BACKUP_FAILS="1").returncode != 0
    status = world.status()
    assert status["ok"] is False and status["message"].startswith("迁移前备份失败")
    assert "pg_dump 出错" in status["message"]
    assert world.head() == good and not any(call.startswith("docker") for call in world.called())
    # 备份成了再上线。
    assert world.deploy().returncode == 0
    assert world.head() != good and world.status()["ok"] is True

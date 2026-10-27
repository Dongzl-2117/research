"""裸 git origin 与假 docker/curl；验证部署输入、协作互斥和回退，绝不连接真实服务。"""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f:
    f.write('docker ' + ' '.join(args) + '\n')
if args[0] == 'inspect':
    if args[2] == '{{.Image}}':
        print('sha256:old-image')
    else:
        print(json.dumps([{'Destination':'/app/config', 'Source':os.environ['OLD_CONFIG']}]))
    sys.exit(0)
if args[-3:] == ['ps', '-q', 'app']:
    print('old-container')
    sys.exit(0)
if 'build' in args and args[-2:] == ['build', 'app']:
    if os.environ.get('DOCKER_FAILS') == '1':
        sys.exit('镜像没建成')
    override = Path(args[args.index('-f', args.index('-f')+1)+1])
    data = json.loads(override.read_text())['services']['app']
    source = Path(data['build']['context'])
    files = {str(p.relative_to(source)):p.read_text() for p in source.rglob('*') if p.is_file()}
    Path(os.environ['BUILT']).write_text(json.dumps({'files':files, 'data':data}))
if 'run' in args:
    if os.environ.get('GUARD_FAIL') == '1':
        sys.exit('数据库版本不符')
    if os.environ.get('GUARD_BUSY') == '1':
        print('busy', flush=True)
        sys.exit(75)
    print('ready', flush=True)
    sys.stdin.readline()
if 'up' in args:
    override = Path(args[args.index('-f', args.index('-f')+1)+1])
    data = json.loads(override.read_text())['services']['app']
    sha = data['environment']['RESEARCH_BUILD_SHA']
    Path(os.environ['LIVE']).write_text(sha)
"""
FAKE_CURL = r"""#!/usr/bin/env python3
import json, os, sys, subprocess
from pathlib import Path
ci = 'api.github.com' in ' '.join(sys.argv)
with open(os.environ['CALLS'],'a') as f:
    f.write('curl ci\n' if ci else 'curl app\n')
if ci:
    path = Path(os.environ['CI_JSON'])
    if not path.exists(): sys.exit(22)
    data = json.loads(path.read_text())
    sha = sys.argv[-1].split('/commits/')[1].split('/')[0]
    for run in data['check_runs']:
        run.setdefault('head_sha', sha)
    # 检查完成后移动本地远端引用，构建仍须使用先前解析的目标。
    if os.environ.get('MOVE_REF'):
        subprocess.run(['git','-C',os.environ['REPO'],'update-ref','refs/remotes/origin/main',os.environ['MOVE_REF']],check=True)
    print(json.dumps(data))
else:
    live = Path(os.environ['LIVE'])
    sha = live.read_text() if live.exists() else 'old-sha'
    if os.environ.get('HEALTH_FAIL') == '1' and sha != 'old-sha': sys.exit(22)
    print(json.dumps({'status':'ok','deployment_protocol':int(os.environ.get('PROTOCOL','1')),'build_sha':sha}))
"""


def _git(cwd, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=测试", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


class World:
    def __init__(self, tmp):
        self.tmp = tmp
        self.origin, self.work, self.mini = (tmp / name for name in ("origin.git", "work", "mini"))
        self.calls, self.ci = tmp / "calls", tmp / "ci.json"
        self.built, self.live = tmp / "built.json", tmp / "live"
        bins = tmp / "bin"
        _write(bins / "docker", FAKE_DOCKER, 0o755)
        _write(bins / "curl", FAKE_CURL, 0o755)
        self.path = str(bins) + os.pathsep + os.environ["PATH"]
        _git(tmp, "init", "--bare", "-b", "main", str(self.origin))
        _git(tmp, "clone", str(self.origin), str(self.work))
        for name in ("deploy.sh", "deploy.py", "status.sh", "host_lock.py", "with_task_lock.py"):
            _write(self.work / "scripts" / name, (ROOT / "scripts" / name).read_text(), 0o755)
        _write(self.work / "docker-compose.yml", "services: {}\n")
        _write(self.work / "config/sources.yaml", "sources: []\n")
        self.commit("初始版本")
        _git(tmp, "clone", "-b", "main", str(self.origin), str(self.mini))

    def commit(self, subject, path="notes.txt"):
        file = self.work / path
        _write(file, (file.read_text() if file.exists() else "") + subject + "\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", subject)
        _git(self.work, "push", "origin", "main")
        return _git(self.work, "rev-parse", "HEAD")

    def ci_says(self, status, conclusion=None):
        self.ci.write_text(
            json.dumps(
                {
                    "check_runs": [
                        {
                            "id": 1,
                            "name": "Tests",
                            "app": {"slug": "github-actions"},
                            "status": status,
                            "conclusion": conclusion,
                        }
                    ]
                }
            )
        )

    def deploy(self, **env):
        self.calls.write_text("")
        full = {
            **os.environ,
            "PATH": self.path,
            "REPO": str(self.mini),
            "CI_JSON": str(self.ci),
            "CALLS": str(self.calls),
            "DEPLOY_REPO": "example/research",
            "APP_URL": "http://app.test/health",
            "OLD_CONFIG": str(self.mini / "config"),
            "BUILT": str(self.built),
            "LIVE": str(self.live),
            "DEPLOY_WAIT": "0",
            **env,
        }
        return subprocess.run(["bash", str(self.mini / "scripts/deploy.sh")], env=full, text=True, capture_output=True)

    def called(self):
        return self.calls.read_text().splitlines()

    def status(self):
        path = self.mini / "data/status/deploy.json"
        return json.loads(path.read_text()) if path.exists() else None


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_green_sha_uses_only_archive_and_preserves_user_work(world):
    world.ci_says("completed", "success")
    sha = world.commit("验证目标")
    _git(world.mini, "fetch", "origin")
    _git(world.mini, "merge", "--ff-only", sha)
    _write(world.mini / "local.py", "本地提交")
    _git(world.mini, "add", "local.py")
    _git(world.mini, "commit", "-m", "未推送提交")
    head = _git(world.mini, "rev-parse", "HEAD")
    _write(world.mini / "untracked.py", "未跟踪")
    _write(world.mini / "config/sources.yaml", "本地未提交修改")
    result = world.deploy(MOVE_REF=head)
    assert result.returncode == 0, result.stdout + result.stderr
    built = json.loads(world.built.read_text())
    assert built["data"]["image"] == f"research-app:{sha}"
    assert built["data"]["build"]["labels"]["org.opencontainers.image.revision"] == sha
    assert built["files"]["notes.txt"].endswith("验证目标\n")
    assert built["files"]["config/sources.yaml"] == "sources: []\n"
    assert "local.py" not in built["files"] and "untracked.py" not in built["files"]
    assert _git(world.mini, "rev-parse", "HEAD") == head
    assert (world.mini / "untracked.py").read_text() == "未跟踪"
    assert (world.mini / "config/sources.yaml").read_text() == "本地未提交修改"
    assert world.live.read_text() == sha and world.status()["ok"]
    manifest = world.mini / "data/releases" / sha / "release.json"
    config = Path(json.loads(manifest.read_text())["services"]["app"]["volumes"][-1].split(":")[0])
    assert config.is_dir()
    assert world.deploy().returncode == 0 and not world.called()


@pytest.mark.parametrize(
    "status,conclusion",
    [("in_progress", None), ("completed", "failure"), ("completed", "skipped"), ("completed", "cancelled")],
)
def test_force_never_bypasses_ci(world, status, conclusion):
    world.ci_says(status, conclusion)
    assert world.deploy(FORCE="1").returncode == 0
    assert not world.built.exists()
    assert world.called() == ["curl ci"]


def test_latest_matching_ci_must_pass(world):
    world.ci_says("completed", "success")
    data = json.loads(world.ci.read_text())
    data["check_runs"].append({**data["check_runs"][0], "id": 2, "conclusion": "failure"})
    world.ci.write_text(json.dumps(data))
    world.deploy()
    assert not world.built.exists()
    assert not world.status()["ok"]


@pytest.mark.parametrize("field,value", [("head_sha", "another-sha"), ("name", "Other"), ("app", {"slug": "unknown"})])
def test_unrelated_success_is_not_accepted(world, field, value):
    world.ci_says("completed", "success")
    data = json.loads(world.ci.read_text())
    data["check_runs"][0][field] = value
    world.ci.write_text(json.dumps(data))
    world.deploy()
    assert not world.built.exists()


def test_build_failure_retries_three_times_without_restarting(world):
    world.ci_says("completed", "success")
    for _ in range(3):
        assert world.deploy(DOCKER_FAILS="1").returncode != 0
        assert not world.live.exists() and not world.status()["ok"]
    assert world.deploy().returncode == 0 and world.called() == []
    assert world.deploy(FORCE="1").returncode == 0 and world.status()["ok"]


def test_legacy_app_requires_manual_handoff(world):
    world.ci_says("completed", "success")
    assert world.deploy(PROTOCOL="0").returncode != 0
    assert "人工交接" in world.status()["message"] or "自动上线失败" in world.status()["message"]
    assert not world.built.exists()


@pytest.mark.parametrize("env", [{"GUARD_BUSY": "1"}, {"GUARD_FAIL": "1"}])
def test_database_task_or_pending_migration_prevents_switch(world, env):
    world.ci_says("completed", "success")
    result = world.deploy(**env)
    assert result.returncode == (0 if "GUARD_BUSY" in env else 1)
    assert not world.live.exists()
    assert not any(" up " in call for call in world.called())


def test_failed_health_restores_exact_old_image_and_reports_code_only(world):
    world.ci_says("completed", "success")
    assert world.deploy(HEALTH_FAIL="1").returncode == 1
    assert world.live.read_text() == "old-sha"
    calls = world.called()
    assert len([c for c in calls if " up " in c]) == 2
    assert not any("--build" in c or "down" in c or "pg_restore" in c for c in calls)
    assert "没有执行数据恢复" in world.status()["message"] or not world.status()["ok"]


def test_host_jobs_and_deployment_exclude_each_other(world):
    lock = world.mini / "data/status/tasks.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        world.ci_says("completed", "success")
        assert world.deploy().returncode == 0
        assert world.called() == [] and not world.live.exists()
        fcntl.flock(handle, fcntl.LOCK_EX)
        env = {**os.environ, "REPO": str(world.mini)}
        marker = world.tmp / "task-started"
        result = subprocess.run(
            [
                sys.executable,
                str(world.mini / "scripts/with_task_lock.py"),
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
            env=env,
        )
        assert result.returncode == 0 and not marker.exists()
    assert world.deploy().returncode == 0 and world.status()["ok"]


def test_outage_recovery_clears_status_without_rebuild(world):
    assert world.deploy().returncode == 0
    assert not world.status()["ok"]
    world.ci_says("completed", "success")
    assert world.deploy().returncode == 0
    away = world.tmp / "away.git"
    world.origin.rename(away)
    seen = world.mini / "data/status/.deploy-seen"
    os.utime(seen, (0, 0))
    assert world.deploy().returncode == 0 and not world.status()["ok"]
    away.rename(world.origin)
    assert world.deploy().returncode == 0 and world.status()["ok"]
    assert world.called() == []

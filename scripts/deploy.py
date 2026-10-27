"""固定 SHA 的部署控制器。只替换 app；不执行迁移，不修改维护者的工作区。"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from host_lock import host_lock


class DeployError(RuntimeError):
    pass


def command(*args: str, **kwargs) -> str:
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise DeployError(f"{args[0]} 失败：{result.stderr.strip().splitlines()[-1:]} ")
    return result.stdout.strip()


class Deployment:
    def __init__(self):
        self.repo = Path(os.environ["REPO"]).resolve()
        self.status = Path(os.environ.get("STATUS_DIR", self.repo / "data/status"))
        self.status.mkdir(parents=True, exist_ok=True)
        self.last = self.status / ".deploy-last"
        self.seen = self.status / ".deploy-seen"
        self.branch = os.environ.get("DEPLOY_BRANCH", "main")
        self.url = os.environ.get("APP_URL", "http://127.0.0.1:8088/health")
        self.wait = int(os.environ.get("DEPLOY_WAIT", "600"))
        self.base = [
            "docker",
            "compose",
            "--project-directory",
            str(self.repo),
            "-f",
            str(self.repo / "docker-compose.yml"),
        ]
        self.compose = list(self.base)

    def git(self, *args) -> str:
        return command("git", "-C", str(self.repo), *args, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})

    def report(self, ok: bool, message: str):
        payload = {"task": "deploy", "ok": ok, "at": datetime.now(UTC).isoformat(), "message": message}
        temp = self.status / f".deploy-{os.getpid()}.json"
        temp.write_text(json.dumps(payload, ensure_ascii=False) + "\n")
        temp.replace(self.status / "deploy.json")
        print(message, file=sys.stdout if ok else sys.stderr, flush=True)

    def unavailable(self, message: str):
        if not self.seen.exists() or time.time() - self.seen.stat().st_mtime > 6 * 3600:
            self.report(False, message)
        else:
            print(message + "，下一轮再试", flush=True)

    def health(self) -> dict:
        try:
            return json.loads(command("curl", "-fsS", "--max-time", "5", self.url))
        except (DeployError, ValueError):
            return {}

    def ci(self, target: str) -> str:
        slug = os.environ.get("DEPLOY_REPO") or self.git("remote", "get-url", "origin").removesuffix(".git")
        slug = slug.removeprefix("https://github.com/").removeprefix("git@github.com:")
        check = os.environ.get("DEPLOY_CHECK", "Tests")
        try:
            data = json.loads(
                command(
                    "curl",
                    "-fsS",
                    "--max-time",
                    "20",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"https://api.github.com/repos/{slug}/commits/{target}/check-runs?check_name={check}",
                )
            )
            runs = [
                r
                for r in data["check_runs"]
                if r.get("name") == check
                and r.get("head_sha") == target
                and (r.get("app") or {}).get("slug") == "github-actions"
            ]
        except (DeployError, KeyError, TypeError, ValueError):
            return "unknown"
        if not runs:
            return "pending"
        latest = max(runs, key=lambda r: r.get("id", 0))
        if latest.get("status") != "completed" or latest.get("conclusion") in {"cancelled", "skipped", "stale"}:
            return "pending"
        return "success" if latest.get("conclusion") == "success" else "failure"

    def run(self):
        with host_lock(self.repo, exclusive=True) as acquired:
            if not acquired:
                print("宿主机任务或上一轮部署还在运行，等下一轮", flush=True)
                return
            self.deploy()

    def deploy(self):
        try:
            self.git(
                "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60", "fetch", "--quiet", "origin", self.branch
            )
        except DeployError:
            self.unavailable(f"取不到 origin/{self.branch}")
            return
        # FETCH_HEAD 当场解析为对象 ID；后续不再解析会移动的 origin/main。
        target = self.git("rev-parse", "FETCH_HEAD^{commit}")
        short = target[:7]
        last_sha, state = self.last.read_text().split() if self.last.exists() else ("", "")
        if target == last_sha and state == "ok":
            self.seen.touch()
            self.report(True, f"已部署 {short}")
            return
        tries = int(state) if target == last_sha and state.isdigit() else 0
        if tries >= 3 and os.environ.get("FORCE") != "1":
            return
        ci = self.ci(target)
        if ci == "unknown":
            self.unavailable(f"查不到 {short} 的 CI 结果")
            return
        self.seen.touch()
        if ci != "success":
            if ci == "failure":
                self.report(False, f"{short} CI 没通过，没有上线")
            else:
                age = time.time() - int(self.git("log", "-1", "--format=%ct", target))
                if age > 7200:
                    self.report(False, f"{short} 两小时还没有通过的 CI 结果，没有上线")
                print(f"{short} 的 CI 还未通过，等下一轮", flush=True)
            return
        # 老版本没有协调协议，无法证明独立 loop 已被保护，默认拒绝首次自动切换。
        current = self.health()
        if current.get("deployment_protocol") != 1:
            raise DeployError("运行中应用尚无部署协调协议；先按 docs/deploy-safety.md 完成有备份的人工交接")
        old_container = command(*self.base, "ps", "-q", "app")
        if not old_container:
            raise DeployError("没有可回退的运行中 app，拒绝自动部署")
        old_image = command("docker", "inspect", "--format", "{{.Image}}", old_container)
        mounts = json.loads(command("docker", "inspect", "--format", "{{json .Mounts}}", old_container))
        old_config = next((m["Source"] for m in mounts if m["Destination"] == "/app/config"), "")
        if not old_config:
            raise DeployError("缺少旧配置挂载，无法安全回退")
        with tempfile.TemporaryDirectory(prefix="research-release-") as directory:
            release = Path(directory)
            archive = release / "source.tar"
            self.git("archive", "--format=tar", f"--output={archive}", target)
            source = release / "source"
            source.mkdir()
            with tarfile.open(archive) as tar:
                tar.extractall(source, filter="data")
            override = release / "release.json"
            image = f"research-app:{target}"
            override.write_text(
                json.dumps(
                    {
                        "services": {
                            "app": {
                                "image": image,
                                "build": {
                                    "context": str(source),
                                    "labels": {"org.opencontainers.image.revision": target},
                                },
                                "environment": {"RESEARCH_BUILD_SHA": target},
                                "volumes": [f"{self.repo / 'data'}:/data", f"{source / 'config'}:/app/config:ro"],
                            }
                        }
                    }
                )
            )
            # compose 文件及配置也来自快照；project-directory 只为延续原来的项目名和 .env。
            self.compose = [
                "docker",
                "compose",
                "--project-directory",
                str(self.repo),
                "-f",
                str(source / "docker-compose.yml"),
                "-f",
                str(override),
            ]
            try:
                command(*self.compose, "build", "app")
            except DeployError:
                self.last.write_text(f"{target} {tries + 1}\n")
                raise
            # 必须把 config 留到容器退出后：临时构建目录不能作为运行时挂载。
            config = self.repo / "data/releases" / target / "config"
            if config.exists():
                expected = {
                    p.relative_to(source / "config"): p.read_bytes()
                    for p in (source / "config").rglob("*")
                    if p.is_file()
                }
                actual = {p.relative_to(config): p.read_bytes() for p in config.rglob("*") if p.is_file()}
                if actual != expected:
                    raise DeployError("发布配置目录与 SHA 不符，禁止覆盖运行中挂载")
            else:
                config.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source / "config", config)
            data = json.loads(override.read_text())
            data["services"]["app"]["volumes"][-1] = f"{config}:/app/config:ro"
            override.write_text(json.dumps(data))
            # 独立容器持有数据库独占锁；不会被 up app 替换。预检只读，迁移缺失就退出。
            with tempfile.TemporaryFile(mode="w+") as errors:
                guard = subprocess.Popen(
                    [
                        *self.compose,
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "app",
                        "python",
                        "-m",
                        "researchlib.coordination",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                )
                try:
                    ready, _, _ = select.select([guard.stdout], [], [], 30)
                    message = guard.stdout.readline().strip() if ready else ""
                    if message == "busy":
                        print("数据库任务仍在运行，等下一轮", flush=True)
                        return
                    if message != "ready":
                        raise DeployError("部署锁或数据库版本预检失败；没有切换应用，请核对迁移与备份")
                    if guard.poll() is not None:
                        raise DeployError("部署锁进程已退出，没有切换应用")
                    self.last.write_text(f"{target} {tries + 1}\n")
                    try:
                        command(*self.compose, "up", "-d", "--no-deps", "--no-build", "app")
                        deadline = time.monotonic() + self.wait
                        while True:
                            found = self.health()
                            if found.get("status") == "ok" and found.get("build_sha") == target:
                                break
                            if time.monotonic() >= deadline:
                                raise DeployError("新镜像健康检查未通过")
                            time.sleep(5)
                    except DeployError as exc:
                        rollback = release / "rollback.json"
                        # 使用当前容器的确切镜像 ID 和原来持久化的 config 挂载。
                        rollback.write_text(
                            json.dumps(
                                {
                                    "services": {
                                        "app": {
                                            "image": old_image,
                                            "environment": {"RESEARCH_BUILD_SHA": current.get("build_sha", "")},
                                            "volumes": [f"{self.repo / 'data'}:/data", f"{old_config}:/app/config:ro"],
                                        }
                                    }
                                }
                            )
                        )
                        command(*self.base, "-f", str(rollback), "up", "-d", "--no-deps", "--no-build", "app")
                        deadline = time.monotonic() + self.wait
                        while True:
                            restored = self.health()
                            if restored.get("status") == "ok" and restored.get("build_sha") == current.get("build_sha"):
                                break
                            if time.monotonic() >= deadline:
                                break
                            time.sleep(5)
                        healthy = restored.get("status") == "ok" and restored.get("build_sha") == current.get(
                            "build_sha"
                        )
                        state = "旧镜像健康检查通过" if healthy else "旧镜像尚未恢复健康，需人工处理"
                        raise DeployError(f"{exc}；已请求代码回退，{state}；没有执行数据恢复") from exc
                    # 留下可由 mini/GitHub 独立使用的运行配置；不需要本次临时目录。
                    saved = config.parent
                    shutil.copyfile(source / "docker-compose.yml", saved / "docker-compose.yml")
                    manifest = json.loads(override.read_text())
                    manifest["services"]["app"].pop("build")
                    (saved / "release.json").write_text(json.dumps(manifest, ensure_ascii=False) + "\n")
                    self.last.write_text(f"{target} ok\n")
                    self.report(True, f"上线 {short}：{self.git('log', '-1', '--format=%s', target)}")
                finally:
                    try:
                        guard.communicate("release\n", timeout=10)
                    except (subprocess.TimeoutExpired, BrokenPipeError):
                        # 只终止本次独立的锁进程，从不杀工作任务。
                        guard.terminate()
                        guard.wait(timeout=10)


if __name__ == "__main__":
    deployment = Deployment()
    try:
        deployment.run()
    except (DeployError, OSError, ValueError) as exc:
        deployment.report(False, str(exc))
        raise SystemExit(1) from None
